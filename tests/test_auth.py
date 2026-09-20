"""
Tests for optional API-key auth (app/auth.py).

The rest of the suite runs with auth disabled (settings.api_key == ""),
which is what lets every other test in this project avoid needing to
know a secret. These tests exercise the auth mechanism itself by
overriding get_settings for just this file.
"""
import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.config import Settings, get_settings

TEST_API_KEY = "test-secret-key-123"


def _settings_with_auth():
    return Settings(api_key=TEST_API_KEY, database_url="sqlite:///:memory:")


@pytest.fixture
def auth_client(client):
    """Same app/db overrides as the shared `client` fixture, but with
    auth turned on for the duration of this fixture only."""
    app.dependency_overrides[get_settings] = _settings_with_auth
    yield client
    del app.dependency_overrides[get_settings]


def test_create_ticket_rejected_without_api_key(auth_client):
    resp = auth_client.post("/tickets", json={"description": "test"})
    assert resp.status_code == 401


def test_create_ticket_rejected_with_wrong_api_key(auth_client):
    resp = auth_client.post(
        "/tickets", json={"description": "test"}, headers={"X-API-Key": "wrong-key"}
    )
    assert resp.status_code == 401


def test_create_ticket_succeeds_with_correct_api_key(auth_client):
    resp = auth_client.post(
        "/tickets", json={"description": "test"}, headers={"X-API-Key": TEST_API_KEY}
    )
    assert resp.status_code == 201


def test_read_endpoints_do_not_require_api_key(auth_client):
    # GET /tickets and GET /health stay open even with auth enabled -
    # only write endpoints (POST/PATCH) are gated.
    assert auth_client.get("/tickets").status_code == 200
    assert auth_client.get("/health").status_code == 200


def test_auth_disabled_by_default(client):
    # The plain `client` fixture (no auth override) should allow writes
    # with no header at all - this is the default the rest of the suite
    # relies on.
    resp = client.post("/tickets", json={"description": "no auth needed"})
    assert resp.status_code == 201
