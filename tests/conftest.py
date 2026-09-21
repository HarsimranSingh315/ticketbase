"""
Shared test fixtures.

Why this file exists (a second real debugging story, on top of the one
in the README): with two test files (test_tickets.py, test_supportrag.py)
each defining their OWN engine and their own
`app.dependency_overrides[get_db] = override_get_db` at module level,
running the full suite together broke in a way that running each file
alone did not.

Cause: `app.dependency_overrides[...] = ...` runs at import time, not
per-test. Pytest imports/collects every test file before running any
test. So whichever test file happened to be imported LAST silently
overwrote the other file's override for the ENTIRE session - meaning
test_tickets.py's requests could end up hitting test_supportrag.py's
separate in-memory SQLite engine, one that test_tickets.py's own
per-test fixture never ran `create_all()` against. Result:
`no such table: tickets`, but only when running the full suite - each
file passed fine in isolation, which is what made it confusing at
first.

Fix: define the engine, session, and override ONCE here in conftest.py,
shared by every test file, so there's only one override to clobber.

Note on `get_rag_index`: it's overridden here for a related reason -
see `override_get_rag_index`'s docstring below.
"""
import os

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.main import app, get_rag_index
from app.database import Base, get_db
from app.supportrag import build_rag_index

# Defaults to a fast in-memory SQLite DB for local dev. Set
# TEST_DATABASE_URL to a real Postgres URL to run the exact same suite
# against Postgres instead - this is what CI does (see
# .github/workflows/ci.yml), since the brief is explicit that Postgres
# should be authoritative for anything concurrency-sensitive, and
# SQLite's more forgiving behavior (e.g. no real ALTER TABLE constraint
# support - see the Alembic migration debugging story in
# docs/current-state.md) can hide real bugs. SQLite remains the local
# default because it needs zero setup - a contributor without Postgres
# installed can still run the full suite.
TEST_DB_URL = os.environ.get("TEST_DATABASE_URL", "sqlite:///:memory:")

if TEST_DB_URL.startswith("sqlite"):
    engine = create_engine(
        TEST_DB_URL,
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
else:
    # Postgres (or anything else): no SQLite-only connect_args/pool -
    # psycopg2 doesn't accept check_same_thread, and StaticPool would
    # serialize what should be a normal connection pool.
    engine = create_engine(TEST_DB_URL)

TestSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


def override_get_db():
    db = TestSessionLocal()
    try:
        yield db
    finally:
        db.close()


def override_get_rag_index():
    """
    Tests don't run the app's lifespan (no `with TestClient(app) as c:`),
    so `request.app.state.rag_index` is never set by the real
    `get_rag_index`. This override builds a fresh RAGIndex straight from
    the TEST database instead - re-seeding+re-fitting per test is cheap
    (16 short articles), and it also means SupportRAG tests never touch
    the real dev/prod SQLite file the way running the real lifespan
    against `app.database.engine` would.
    """
    db = TestSessionLocal()
    try:
        return build_rag_index(db)
    finally:
        db.close()


app.dependency_overrides[get_db] = override_get_db
app.dependency_overrides[get_rag_index] = override_get_rag_index


@pytest.fixture(autouse=True)
def setup_and_teardown():
    Base.metadata.create_all(bind=engine)
    yield
    Base.metadata.drop_all(bind=engine)
    # slowapi's in-memory rate-limit storage persists across the whole
    # pytest process, not per-test - without resetting it, tests that
    # log in repeatedly (each with its own admin_client/agent_client/
    # reviewer_client fixture) eventually trip the real login rate
    # limit and fail with 429, which looks like a login bug but isn't
    # one. Found by running the full auth test file together (each
    # test passed in isolation) - the same class of "only fails
    # together" issue as the cross-file dependency_overrides bug this
    # file's own docstring describes, different mechanism.
    app.state.limiter.reset()


@pytest.fixture
def client():
    return TestClient(app)


@pytest.fixture
def db_session():
    """Direct DB session bound to the shared in-memory test engine -
    for tests that need to manipulate the database directly rather
    than through the API (e.g. simulating a pre-existing DB state)."""
    db = TestSessionLocal()
    try:
        yield db
    finally:
        db.close()


# --- Agent auth fixtures (Milestone 1) ---
#
# Tests never run the app's real `lifespan`, so the startup bootstrap-
# admin mechanism (app/main.py's _bootstrap_admin_if_needed) never runs
# here either - see override_get_rag_index's docstring above for the
# same reasoning applied to a different piece of startup. These
# fixtures create a real admin/agent user directly, the same way the
# bootstrap mechanism would, so tests can exercise the now-protected
# UI routes without re-deriving login from scratch every time.

TEST_ADMIN_EMAIL = "test-admin@example.com"
TEST_ADMIN_PASSWORD = "test-admin-password-123"
TEST_AGENT_EMAIL = "test-agent@example.com"
TEST_AGENT_PASSWORD = "test-agent-password-123"
TEST_REVIEWER_EMAIL = "test-reviewer@example.com"
TEST_REVIEWER_PASSWORD = "test-reviewer-password-123"


@pytest.fixture
def admin_client(client, db_session):
    """A TestClient already logged in as a real admin user."""
    from app import crud as _crud
    from app.models import UserRole
    _crud.create_user(db_session, email=TEST_ADMIN_EMAIL, name="Test Admin", password=TEST_ADMIN_PASSWORD, role=UserRole.admin)
    resp = client.post("/login", data={"email": TEST_ADMIN_EMAIL, "password": TEST_ADMIN_PASSWORD})
    assert resp.status_code in (200, 303), f"admin login failed in fixture: {resp.status_code}"
    return client


@pytest.fixture
def agent_client(client, db_session):
    """A TestClient already logged in as a real agent (non-admin) user."""
    from app import crud as _crud
    from app.models import UserRole
    _crud.create_user(db_session, email=TEST_AGENT_EMAIL, name="Test Agent", password=TEST_AGENT_PASSWORD, role=UserRole.agent)
    resp = client.post("/login", data={"email": TEST_AGENT_EMAIL, "password": TEST_AGENT_PASSWORD})
    assert resp.status_code in (200, 303), f"agent login failed in fixture: {resp.status_code}"
    return client


@pytest.fixture
def reviewer_client(client, db_session):
    """A TestClient already logged in as a read-only reviewer."""
    from app import crud as _crud
    from app.models import UserRole
    _crud.create_user(db_session, email=TEST_REVIEWER_EMAIL, name="Test Reviewer", password=TEST_REVIEWER_PASSWORD, role=UserRole.reviewer)
    resp = client.post("/login", data={"email": TEST_REVIEWER_EMAIL, "password": TEST_REVIEWER_PASSWORD})
    assert resp.status_code in (200, 303), f"reviewer login failed in fixture: {resp.status_code}"
    return client


def get_csrf_token(authed_client, path="/"):
    """
    Scrapes the csrf_token hidden field out of a real rendered page,
    rather than recomputing the HMAC independently in the test - this
    way a test verifies the actual value the app produces, not a
    parallel implementation that could drift from it and stay green.
    """
    import re
    resp = authed_client.get(path)
    match = re.search(r'name="csrf_token" value="([^"]+)"', resp.text)
    assert match, f"No csrf_token field found on {path} (status {resp.status_code})"
    return match.group(1)
