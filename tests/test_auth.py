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


def test_read_endpoints_require_authentication(auth_client):
    """
    GET /tickets now requires either a real session or the API key -
    this was a genuine P0 finding (see docs/current-state.md's
    production-readiness section): these routes previously returned
    real ticket data to fully anonymous requests, even with an API key
    configured, since require_api_key was never applied to them at
    all. /health stays deliberately public - a health check has to be
    reachable without credentials, by design, not by oversight.
    """
    assert auth_client.get("/tickets").status_code == 401
    assert auth_client.get("/tickets", headers={"X-API-Key": TEST_API_KEY}).status_code == 200
    assert auth_client.get("/health").status_code == 200


def test_the_four_previously_open_routes_all_reject_anonymous_requests_with_no_api_key_configured(client):
    """
    The core of the P0 fix, stated as directly as possible: with NO
    API_KEY configured at all (the plain local/dev/test default - the
    exact condition under which these routes were previously wide
    open), all four routes the review flagged now reject a fully
    anonymous request. Before this fix, every one of these calls
    returned 200 with real ticket data instead of 401.
    """
    # A ticket has to exist for the per-ticket routes to have something
    # real to (fail to) return - created via the one write route that
    # was never part of this vulnerability (POST /tickets already had
    # require_api_key, which correctly no-ops when unset, same as always).
    ticket_id = client.post("/tickets", json={"description": "test"}).json()["id"]

    assert client.get("/tickets").status_code == 401
    assert client.get(f"/tickets/{ticket_id}").status_code == 401
    assert client.get(f"/tickets/{ticket_id}/related").status_code == 401
    assert client.post(f"/tickets/{ticket_id}/suggest").status_code == 401


def test_the_four_previously_open_routes_work_with_a_real_session(admin_client):
    """The other half of the fix: a genuinely logged-in browser session
    (what app.js's same-origin fetch now sends) must still work
    normally - this isn't a lockout, it's closing an unintended
    anonymous-access path."""
    ticket_id = admin_client.post("/tickets", json={"description": "My VPN will not connect"}).json()["id"]

    assert admin_client.get("/tickets").status_code == 200
    assert admin_client.get(f"/tickets/{ticket_id}").status_code == 200
    assert admin_client.get(f"/tickets/{ticket_id}/related").status_code == 200
    assert admin_client.post(f"/tickets/{ticket_id}/suggest").status_code == 200


def test_the_four_previously_open_routes_work_with_a_valid_api_key(auth_client):
    """And machine/CLI clients using the API key (not a session) must
    also still work - this is the "scoped service credentials for
    machine clients" half of the review's recommended fix."""
    ticket_id = auth_client.post(
        "/tickets", json={"description": "test"}, headers={"X-API-Key": TEST_API_KEY}
    ).json()["id"]

    headers = {"X-API-Key": TEST_API_KEY}
    assert auth_client.get("/tickets", headers=headers).status_code == 200
    assert auth_client.get(f"/tickets/{ticket_id}", headers=headers).status_code == 200
    assert auth_client.get(f"/tickets/{ticket_id}/related", headers=headers).status_code == 200
    assert auth_client.post(f"/tickets/{ticket_id}/suggest", headers=headers).status_code == 200


def test_wrong_api_key_still_rejected_on_the_four_routes(auth_client):
    ticket_id = auth_client.post(
        "/tickets", json={"description": "test"}, headers={"X-API-Key": TEST_API_KEY}
    ).json()["id"]
    wrong = {"X-API-Key": "not-the-real-key"}
    assert auth_client.get("/tickets", headers=wrong).status_code == 401
    assert auth_client.get(f"/tickets/{ticket_id}", headers=wrong).status_code == 401


def test_auth_disabled_by_default(client):
    # The plain `client` fixture (no auth override) should allow writes
    # with no header at all - this is the default the rest of the suite
    # relies on.
    resp = client.post("/tickets", json={"description": "no auth needed"})
    assert resp.status_code == 201


def test_ui_category_write_rejected_without_login(client, db_session):
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
        data={"category": "hardware", "csrf_token": "irrelevant-no-session-exists", "version": ticket["version"]},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert "/login" in resp.headers["location"]

    # Confirm it genuinely did not persist, not just that the response
    # code looked right - checked directly against the DB, not via
    # another API call, since GET /tickets/{id} now requires auth too
    # (a deliberate fix, not something this test should route around).
    from app import crud
    check = crud.get_ticket(db_session, ticket["id"])
    assert check.category is None
    assert check.category_confirmed == 0


def test_ui_ticket_creation_rejected_without_login(client):
    resp = client.post(
        "/ui/tickets", data={"description": "should not be created", "csrf_token": "x"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert "/login" in resp.headers["location"]


def test_ui_status_update_rejected_without_login(client, db_session):
    ticket = client.post("/tickets", json={"description": "test"}).json()
    resp = client.post(
        f"/ui/tickets/{ticket['id']}/status", data={"status": "resolved", "csrf_token": "x", "version": ticket["version"]},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert "/login" in resp.headers["location"]
    from app import crud
    check = crud.get_ticket(db_session, ticket["id"])
    assert check.status.value == "open"


def test_login_redirects_to_safe_relative_next_path(admin_client, db_session):
    """The normal, intended case still works after the fix."""
    from app.models import User, UserRole
    from app.security import hash_password
    user = User(email="redirtest1@example.com", name="RedirTest1", password_hash=hash_password("password123"), role=UserRole.agent)
    db_session.add(user)
    db_session.commit()

    resp = admin_client.post(
        "/login", data={"email": "redirtest1@example.com", "password": "password123", "next": "/kb"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert resp.headers["location"] == "/kb"


def test_login_rejects_open_redirect_to_external_site(admin_client, db_session):
    """
    A real open redirect an external review found: `next` was passed
    straight to RedirectResponse with no check at all. An attacker
    could craft a login link with next=https://evil.com and, after a
    real successful login on the real site, send the user on to a
    phishing page - the login itself being genuine is exactly what
    makes this dangerous.
    """
    from app.models import User, UserRole
    from app.security import hash_password
    user = User(email="redirtest2@example.com", name="RedirTest2", password_hash=hash_password("password123"), role=UserRole.agent)
    db_session.add(user)
    db_session.commit()

    for malicious_next in ["https://evil.com/phishing", "http://evil.com", "//evil.com", "/\\evil.com", "javascript:alert(1)"]:
        resp = admin_client.post(
            "/login", data={"email": "redirtest2@example.com", "password": "password123", "next": malicious_next},
            follow_redirects=False,
        )
        assert resp.status_code == 303
        assert resp.headers["location"] == "/", f"expected safe fallback for next={malicious_next!r}, got {resp.headers['location']!r}"
