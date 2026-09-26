"""
Tests for agent identity/auth (Milestone 1): login, sessions, CSRF,
roles, and the invite-only account creation flow.

Everything in this file was FIRST verified manually against a real
PostgreSQL 16 server running in the build sandbox (see
docs/current-state.md for that transcript) before being written here as
permanent, repeatable pytest coverage - the real-Postgres run is what
gave confidence this actually works outside SQLite's more forgiving
behavior, and these tests are what keep it working on every future change.
"""
import re

from tests.conftest import (
    TEST_ADMIN_EMAIL, TEST_ADMIN_PASSWORD,
    TEST_AGENT_EMAIL, TEST_AGENT_PASSWORD,
    TEST_REVIEWER_EMAIL, TEST_REVIEWER_PASSWORD,
    get_csrf_token,
)


# --- Login ---

def test_unauthenticated_access_redirects_to_login(client):
    resp = client.get("/", follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"] == "/login?next=/"


def test_login_with_correct_credentials_succeeds(admin_client):
    # The admin_client fixture itself performs a login - if it didn't
    # work, its own assertion would already have failed. This test
    # confirms the resulting session actually grants access.
    resp = admin_client.get("/")
    assert resp.status_code == 200
    assert "Test Admin" in resp.text


def test_login_with_wrong_password_rejected(client, db_session):
    from app import crud
    from app.models import UserRole
    from tests.conftest import _login
    crud.create_user(db_session, email=TEST_ADMIN_EMAIL, name="Test Admin", password=TEST_ADMIN_PASSWORD, role=UserRole.admin)

    resp = _login(client, TEST_ADMIN_EMAIL, "wrong-password")
    assert resp.status_code == 401
    assert "Incorrect email or password" in resp.text


def test_login_with_unknown_email_rejected_with_generic_message(client):
    """Same error message as a wrong password - a distinct message would
    let an attacker enumerate which emails have accounts."""
    from tests.conftest import _login
    resp = _login(client, "nobody@example.com", "whatever")
    assert resp.status_code == 401
    assert "Incorrect email or password" in resp.text


def test_password_is_never_stored_in_plaintext(db_session):
    from app import crud
    from app.models import UserRole
    user = crud.create_user(db_session, email=TEST_ADMIN_EMAIL, name="Test Admin", password="my-real-password", role=UserRole.admin)
    assert user.password_hash != "my-real-password"
    assert "my-real-password" not in user.password_hash
    assert user.password_hash.startswith("$argon2")


def test_logout_revokes_the_session(admin_client):
    from tests.conftest import get_csrf_token
    csrf = get_csrf_token(admin_client, "/")
    resp = admin_client.post("/logout", data={"csrf_token": csrf}, follow_redirects=False)
    assert resp.status_code == 303

    # Same client (same cookie jar) - the old session cookie is either
    # cleared or, if somehow replayed, must no longer be valid.
    resp2 = admin_client.get("/", follow_redirects=False)
    assert resp2.status_code == 303
    assert "/login" in resp2.headers["location"]


# --- CSRF ---

def test_csrf_token_missing_entirely_rejected(admin_client):
    resp = admin_client.post("/ui/tickets", data={"description": "no csrf field at all"})
    assert resp.status_code == 422  # FastAPI's own Form(...) validation catches this


def test_csrf_token_present_but_wrong_rejected(admin_client):
    resp = admin_client.post("/ui/tickets", data={"description": "test", "csrf_token": "not-the-real-token"})
    assert resp.status_code == 403


def test_csrf_token_correct_succeeds(admin_client):
    csrf = get_csrf_token(admin_client, "/")
    resp = admin_client.post("/ui/tickets", data={"description": "test with real csrf", "csrf_token": csrf}, follow_redirects=False)
    assert resp.status_code == 303


# --- Roles ---

def test_reviewer_can_read_but_not_write(reviewer_client):
    assert reviewer_client.get("/").status_code == 200

    resp = reviewer_client.post("/ui/tickets", data={"description": "reviewer trying to write", "csrf_token": "x"})
    assert resp.status_code == 403


def test_agent_can_create_tickets(agent_client):
    csrf = get_csrf_token(agent_client, "/")
    resp = agent_client.post("/ui/tickets", data={"description": "agent created this", "csrf_token": csrf}, follow_redirects=False)
    assert resp.status_code == 303


def test_non_admin_cannot_access_invite_page(agent_client):
    resp = agent_client.get("/invite")
    assert resp.status_code == 403


def test_admin_can_access_invite_page(admin_client):
    resp = admin_client.get("/invite")
    assert resp.status_code == 200


# --- Invite flow ---

def test_full_invite_and_accept_flow(admin_client, client):
    csrf = get_csrf_token(admin_client, "/invite")
    resp = admin_client.post("/invite", data={"email": "newagent@example.com", "role": "agent", "csrf_token": csrf})
    assert resp.status_code == 200
    token_match = re.search(r"accept-invite\?token=([\w\-]+)", resp.text)
    assert token_match, "invite link not found in response"
    token = token_match.group(1)

    # A completely separate, logged-out client accepts the invite.
    accept_resp = client.post(
        "/accept-invite", data={"token": token, "name": "New Agent", "password": "newagentpassword123"},
        follow_redirects=False,
    )
    assert accept_resp.status_code == 303
    assert "session_id" in accept_resp.cookies

    # The new account can independently log in with its own credentials.
    from fastapi.testclient import TestClient
    from app.main import app
    from tests.conftest import _login
    fresh_client = TestClient(app)
    login_resp = _login(fresh_client, "newagent@example.com", "newagentpassword123")
    assert login_resp.status_code in (200, 303)


def test_invite_cannot_be_used_twice(admin_client, client):
    csrf = get_csrf_token(admin_client, "/invite")
    resp = admin_client.post("/invite", data={"email": "onceonly@example.com", "role": "agent", "csrf_token": csrf})
    token = re.search(r"accept-invite\?token=([\w\-]+)", resp.text).group(1)

    first = client.post("/accept-invite", data={"token": token, "name": "First", "password": "first-password-123"}, follow_redirects=False)
    assert first.status_code == 303

    from fastapi.testclient import TestClient
    from app.main import app
    second_client = TestClient(app)
    second = second_client.post("/accept-invite", data={"token": token, "name": "Second", "password": "second-password-456"})
    assert second.status_code == 400


def test_invalid_invite_token_rejected(client):
    resp = client.get("/accept-invite", params={"token": "totally-made-up-token"})
    assert resp.status_code == 400
    assert "invalid" in resp.text.lower() or "expired" in resp.text.lower()


def test_short_password_rejected_on_invite_acceptance(admin_client, client):
    csrf = get_csrf_token(admin_client, "/invite")
    resp = admin_client.post("/invite", data={"email": "shortpw@example.com", "role": "agent", "csrf_token": csrf})
    token = re.search(r"accept-invite\?token=([\w\-]+)", resp.text).group(1)

    result = client.post("/accept-invite", data={"token": token, "name": "Someone", "password": "short"})
    assert result.status_code == 422
