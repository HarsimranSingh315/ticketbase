"""
Tests for the optional shared API-key auth on the JSON API (app/auth.py's
require_api_key), plus regression tests confirming the browser UI's
write routes are protected too - now by real session auth (Milestone 1),
which replaced an earlier Milestone-0 stopgap. See
tests/test_agent_auth.py for the full session/CSRF/role test suite.

The API-key tests run with auth disabled (settings.api_key == "") for
the rest of the suite, which is what lets every other test in this
project avoid needing to know a secret. These tests exercise the
mechanism itself by overriding get_settings for just this file.
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


def test_ui_category_write_rejected_without_login(client):
    """
    Regression test for a real, confirmed security gap (see
    docs/current-state.md): the JSON API correctly rejected an
    unauthenticated category write, but the browser form route for the
    SAME action succeeded completely and PERSISTED
    category_confirmed=True with zero authentication. Originally closed
    with a Milestone-0 stopgap (reusing the API key, which browsers
    can't practically send); this tests the REAL fix Milestone 1
    replaced it with - a logged-out browser request gets redirected to
    /login, not silently allowed through.
    """
    ticket = client.post("/tickets", json={"description": "battery issue"}).json()

    resp = client.post(
        f"/ui/tickets/{ticket['id']}/category",
        data={"category": "hardware", "csrf_token": "irrelevant-no-session-exists"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert "/login" in resp.headers["location"]

    # Confirm it genuinely did not persist, not just that the response
    # code looked right.
    check = client.get(f"/tickets/{ticket['id']}").json()
    assert check["category"] is None
    assert check["category_confirmed"] is False


def test_ui_ticket_creation_rejected_without_login(client):
    resp = client.post(
        "/ui/tickets", data={"description": "should not be created", "csrf_token": "x"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert "/login" in resp.headers["location"]


def test_ui_status_update_rejected_without_login(client):
    ticket = client.post("/tickets", json={"description": "test"}).json()
    resp = client.post(
        f"/ui/tickets/{ticket['id']}/status", data={"status": "resolved", "csrf_token": "x"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert "/login" in resp.headers["location"]
    check = client.get(f"/tickets/{ticket['id']}").json()
    assert check["status"] == "open"
