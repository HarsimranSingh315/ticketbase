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
"""
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.main import app
from app.database import Base, get_db

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


app.dependency_overrides[get_db] = override_get_db


@pytest.fixture(autouse=True)
def setup_and_teardown():
    Base.metadata.create_all(bind=engine)
    yield
    Base.metadata.drop_all(bind=engine)


@pytest.fixture
def client():
    return TestClient(app)
