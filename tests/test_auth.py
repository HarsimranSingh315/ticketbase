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


def _login_with_next(client, email, password, next_path):
    """Same as conftest's _login, but also carries a `next` value -
    needed for these redirect-specific tests."""
    import re
    login_page = client.get("/login")
    match = re.search(r'name="prelogin_csrf" value="([^"]*)"', login_page.text)
    prelogin_csrf = match.group(1) if match else ""
    return client.post("/login", data={"email": email, "password": password, "next": next_path, "prelogin_csrf": prelogin_csrf}, follow_redirects=False)


def test_login_redirects_to_safe_relative_next_path(admin_client, db_session):
    """The normal, intended case still works after the fix."""
    from app.models import User, UserRole
    from app.security import hash_password
    user = User(email="redirtest1@example.com", name="RedirTest1", password_hash=hash_password("password123"), role=UserRole.agent)
    db_session.add(user)
    db_session.commit()

    resp = _login_with_next(admin_client, "redirtest1@example.com", "password123", "/kb")
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
        resp = _login_with_next(admin_client, "redirtest2@example.com", "password123", malicious_next)
        assert resp.status_code == 303
        assert resp.headers["location"] == "/", f"expected safe fallback for next={malicious_next!r}, got {resp.headers['location']!r}"


def test_session_token_is_hashed_at_rest(client, db_session):
    """
    A real gap an external review found: session tokens were stored as
    plain, directly usable bearer secrets - anyone reading the sessions
    table (a DB leak, an overly-curious query) could impersonate any
    logged-in user immediately, no cracking needed. Verified directly:
    the raw cookie value and the stored DB value must differ, and
    hashing the cookie must produce exactly the stored value.
    """
    from app.models import User, UserRole, Session as SessionModel
    from app.security import hash_password, hash_token
    from tests.conftest import _login
    user = User(email="hashtest1@example.com", name="HashTest1", password_hash=hash_password("password123"), role=UserRole.agent)
    db_session.add(user)
    db_session.commit()

    resp = _login(client, "hashtest1@example.com", "password123")
    cookie_value = client.cookies.get("session_id")
    assert cookie_value is not None

    db_session.expire_all()
    stored = db_session.query(SessionModel).filter(SessionModel.user_id == user.id).first()
    assert stored is not None
    assert stored.id != cookie_value  # never the raw value
    assert stored.id == hash_token(cookie_value)  # always the hash of it

    # And the real cookie still authenticates correctly end-to-end.
    home = client.get("/")
    assert home.status_code == 200


def test_invite_token_is_hashed_at_rest(admin_client, db_session):
    """Same protection, for invite tokens."""
    from app.models import Invite
    from app.security import hash_token
    from tests.conftest import get_csrf_token
    csrf = get_csrf_token(admin_client, "/invite")
    resp = admin_client.post("/invite", data={"email": "newagent@example.com", "role": "agent", "csrf_token": csrf})
    assert resp.status_code == 200
    # The invite link shown to the admin contains the RAW token.
    import re
    match = re.search(r"token=([\w\-]+)", resp.text)
    assert match is not None
    raw_token_from_link = match.group(1)

    db_session.expire_all()
    stored = db_session.query(Invite).filter(Invite.email == "newagent@example.com").first()
    assert stored is not None
    assert stored.token != raw_token_from_link
    assert stored.token == hash_token(raw_token_from_link)

    # And the raw token from the link still redeems correctly.
    accept_page = admin_client.get(f"/accept-invite?token={raw_token_from_link}")
    assert accept_page.status_code == 200


def test_login_without_prelogin_csrf_is_rejected(client, db_session):
    """The actual security property, not just that normal login works:
    a POST to /login missing the double-submit-cookie token (as a
    forged cross-site request necessarily would be, since it can't
    read the victim's cookie to copy the value) must be rejected."""
    from app.models import User, UserRole
    from app.security import hash_password
    user = User(email="csrflogin@example.com", name="CsrfLogin", password_hash=hash_password("password123"), role=UserRole.agent)
    db_session.add(user)
    db_session.commit()

    # No GET /login first, so no cookie exists - and no prelogin_csrf field either.
    resp = client.post("/login", data={"email": "csrflogin@example.com", "password": "password123"})
    assert resp.status_code == 403


def test_login_with_mismatched_prelogin_csrf_is_rejected(client, db_session):
    """Even WITH a cookie present, a submitted value that doesn't match
    it must be rejected - this is what actually makes it a double-
    submit check, not just \"is something present\"."""
    from app.models import User, UserRole
    from app.security import hash_password
    user = User(email="csrflogin2@example.com", name="CsrfLogin2", password_hash=hash_password("password123"), role=UserRole.agent)
    db_session.add(user)
    db_session.commit()

    client.get("/login")  # sets the real cookie
    resp = client.post("/login", data={"email": "csrflogin2@example.com", "password": "password123", "prelogin_csrf": "attacker-guessed-wrong-value"})
    assert resp.status_code == 403


def test_logout_without_csrf_token_is_rejected(admin_client):
    """A forged cross-site POST to /logout (an attacker's page
    submitting a hidden form to a victim's browser) has no way to know
    the victim's real CSRF token - must be rejected, not silently
    logging the victim out. A wholly missing field is FastAPI's own
    422 (the form field is required at all); a present-but-wrong value
    is the 403 from this route's own check - both are genuine
    rejections, tested separately."""
    resp = admin_client.post("/logout", follow_redirects=False)
    assert resp.status_code == 422


def test_logout_with_wrong_csrf_token_is_rejected(admin_client):
    resp = admin_client.post("/logout", data={"csrf_token": "not-the-real-token"}, follow_redirects=False)
    assert resp.status_code == 403
