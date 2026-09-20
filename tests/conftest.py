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
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.main import app, get_rag_index
from app.database import Base, get_db
from app.supportrag import build_rag_index

TEST_DB_URL = "sqlite:///:memory:"
engine = create_engine(
    TEST_DB_URL,
    connect_args={"check_same_thread": False},
    poolclass=StaticPool,
)
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


@pytest.fixture
def client():
    return TestClient(app)
