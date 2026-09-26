"""
User administration, self-service password change, and atomic invite
acceptance. Race tests at the bottom run only on PostgreSQL (see
tests/test_concurrency_races.py for why SQLite can't host them).
"""
import threading

import pytest

from tests.conftest import get_csrf_token, _login, TEST_ADMIN_EMAIL

PW = "a-perfectly-fine-passphrase"


def _user(db, email, role="agent", password=PW):
    from app import crud
    from app.models import UserRole
    return crud.create_user(db, email=email, name=email.split("@")[0], password=password, role=UserRole(role))


def _post(client, path, data):
    data = {**data, "csrf_token": get_csrf_token(client, "/admin/users" if path.startswith("/admin") else "/account/password")}
    return client.post(path, data=data, follow_redirects=False)


# --- Access -------------------------------------------------------------------

def test_non_admins_cannot_reach_user_admin(agent_client, db_session):
    target = _user(db_session, "t@x.test")
    assert agent_client.get("/admin/users").status_code == 403
    csrf = get_csrf_token(agent_client, "/")
    r = agent_client.post(f"/admin/users/{target.id}/active", data={"active": "false", "csrf_token": csrf})
    assert r.status_code == 403


def test_admin_actions_require_csrf(admin_client, db_session):
    target = _user(db_session, "t@x.test")
    r = admin_client.post(f"/admin/users/{target.id}/active", data={"active": "false", "csrf_token": "forged"})
    assert r.status_code == 403


# --- Deactivation ---------------------------------------------------------------

def test_deactivation_ends_sessions_blocks_login_and_unassigns_open_tickets(admin_client, db_session):
    from fastapi.testclient import TestClient
    from app import crud
    from app.main import app
    agent = _user(db_session, "leaver@x.test")
    their_client = TestClient(app)
    assert _login(their_client, "leaver@x.test", PW).status_code in (200, 303)
    assert their_client.get("/").status_code == 200

    open_t = crud.create_ticket(db_session, "open one")
    done_t = crud.create_ticket(db_session, "resolved one")
    open_t.assignee_id = done_t.assignee_id = agent.id
    done_t.status = "resolved"
    db_session.commit()

    r = _post(admin_client, f"/admin/users/{agent.id}/active", {"active": "false"})
    assert r.status_code == 200 and "moved to Unassigned" in r.text

    assert their_client.get("/", follow_redirects=False).status_code in (302, 303, 307)  # existing session dead
    assert _login(TestClient(app), "leaver@x.test", PW).status_code == 401              # can't log back in
    db_session.expire_all()
    assert crud.get_ticket(db_session, open_t.id).assignee_id is None       # back in the shared queue
    assert crud.get_ticket(db_session, done_t.id).assignee_id == agent.id   # history untouched


def test_reactivation_restores_login(admin_client, db_session):
    from fastapi.testclient import TestClient
    from app.main import app
    agent = _user(db_session, "back@x.test")
    _post(admin_client, f"/admin/users/{agent.id}/active", {"active": "false"})
    _post(admin_client, f"/admin/users/{agent.id}/active", {"active": "true"})
    assert _login(TestClient(app), "back@x.test", PW).status_code in (200, 303)


def test_admin_cannot_deactivate_or_demote_themselves(admin_client, db_session):
    from app.models import User
    me = db_session.query(User).filter(User.email == TEST_ADMIN_EMAIL).one()
    assert _post(admin_client, f"/admin/users/{me.id}/active", {"active": "false"}).status_code == 409
    assert _post(admin_client, f"/admin/users/{me.id}/role", {"role": "agent"}).status_code == 409
    db_session.expire_all()
    assert me.is_active and me.role.value == "admin"


def test_last_active_admin_cannot_be_removed(db_session):
    from app import crud
    only_admin = _user(db_session, "boss@x.test", role="admin")
    other = _user(db_session, "other@x.test", role="agent")
    with pytest.raises(crud.AdminActionRefused):
        crud.set_user_role(db_session, only_admin.id, "agent", actor_user_id=other.id)
    with pytest.raises(crud.AdminActionRefused):
        crud.set_user_active(db_session, only_admin.id, False, actor_user_id=other.id)
    db_session.expire_all()
    assert only_admin.is_active and only_admin.role.value == "admin"


def test_role_change_ends_sessions_and_is_audited(admin_client, db_session):
    from fastapi.testclient import TestClient
    from app.main import app
    from app.models import AuditEvent
    agent = _user(db_session, "promo@x.test")
    their = TestClient(app)
    _login(their, "promo@x.test", PW)
    assert _post(admin_client, f"/admin/users/{agent.id}/role", {"role": "reviewer"}).status_code == 200
    assert their.get("/", follow_redirects=False).status_code in (302, 303, 307)
    ev = db_session.query(AuditEvent).filter(AuditEvent.action == "user.role_changed").one()
    assert "reviewer" in ev.details


def test_invalid_role_rejected(admin_client, db_session):
    agent = _user(db_session, "r@x.test")
    assert _post(admin_client, f"/admin/users/{agent.id}/role", {"role": "superuser"}).status_code == 422


def test_admin_can_sign_a_user_out_everywhere(admin_client, db_session):
    from fastapi.testclient import TestClient
    from app.main import app
    agent = _user(db_session, "so@x.test")
    a, b = TestClient(app), TestClient(app)
    _login(a, "so@x.test", PW); _login(b, "so@x.test", PW)
    assert _post(admin_client, f"/admin/users/{agent.id}/revoke-sessions", {}).status_code == 200
    for c in (a, b):
        assert c.get("/", follow_redirects=False).status_code in (302, 303, 307)


# --- Own password -------------------------------------------------------------------

@pytest.mark.parametrize("current,new,confirm,expect", [
    ("wrong-current-pass", "brand-new-passphrase", "brand-new-passphrase", "current password is incorrect"),
    (PW, "short", "short", "12 to 128"),
    (PW, "x" * 129, "x" * 129, "12 to 128"),
    (PW, PW, PW, "different from your current"),
    (PW, "brand-new-passphrase", "different-confirmation", "The two new passwords"),
])
def test_password_change_rejections(client, db_session, current, new, confirm, expect):
    _user(db_session, "pw@x.test")
    _login(client, "pw@x.test", PW)
    r = _post(client, "/account/password", {"current_password": current, "new_password": new, "confirm_password": confirm})
    assert r.status_code == 422 and expect in r.text


def test_password_change_signs_out_other_sessions_but_not_this_one(client, db_session):
    from fastapi.testclient import TestClient
    from app.main import app
    from app.models import AuditEvent
    _user(db_session, "pw2@x.test")
    other = TestClient(app)
    _login(other, "pw2@x.test", PW)
    _login(client, "pw2@x.test", PW)
    new = "a-brand-new-passphrase"
    r = _post(client, "/account/password", {"current_password": PW, "new_password": new, "confirm_password": new})
    assert r.status_code == 200 and "1 other session" in r.text
    assert client.get("/").status_code == 200                                              # still signed in here
    assert other.get("/", follow_redirects=False).status_code in (302, 303, 307)          # signed out there
    assert _login(TestClient(app), "pw2@x.test", PW).status_code == 401                   # old password dead
    assert _login(TestClient(app), "pw2@x.test", new).status_code in (200, 303)
    ev = db_session.query(AuditEvent).filter(AuditEvent.action == "user.password_changed").one()
    assert "argon2" not in (ev.details or "") and new not in (ev.details or "")


# --- Invites ------------------------------------------------------------------------

def _invite(db, email="new@x.test"):
    from app import crud
    from app.models import UserRole
    admin = _user(db, f"inviter-{email}", role="admin")
    raw, _ = crud.create_invite(db, email=email, role=UserRole.agent, invited_by_user_id=admin.id, ttl_hours=24)
    return raw


@pytest.mark.parametrize("name,password", [("   ", PW), ("Real Name", "short-pw"), ("Real Name", "y" * 129)])
def test_invite_acceptance_validates_name_and_password(client, db_session, name, password):
    from app.models import User
    token = _invite(db_session)
    r = client.post("/accept-invite", data={"token": token, "name": name, "password": password})
    assert r.status_code == 422
    assert db_session.query(User).filter(User.email == "new@x.test").count() == 0
    # The invite wasn't consumed by the failed attempt:
    ok = client.post("/accept-invite", data={"token": token, "name": "Real Name", "password": PW}, follow_redirects=False)
    assert ok.status_code == 303


# --- Genuine races (PostgreSQL only) -----------------------------------------------------

postgres_only = pytest.mark.skipif(
    __import__("tests.conftest", fromlist=["engine"]).engine.dialect.name != "postgresql",
    reason="Genuine concurrent race; needs separate real DB connections (PostgreSQL).",
)


def _race(fn_a, fn_b):
    barrier, results, lock = threading.Barrier(2), [], threading.Lock()

    def run(fn):
        barrier.wait()
        try:
            out = ("ok", fn())
        except Exception as exc:  # recorded, not swallowed: asserted on below
            out = ("err", type(exc).__name__)
        with lock:
            results.append(out)

    ts = [threading.Thread(target=run, args=(f,)) for f in (fn_a, fn_b)]
    [t.start() for t in ts]; [t.join() for t in ts]
    return results


@postgres_only
def test_two_admins_demoting_each_other_simultaneously_leaves_one_admin(db_session):
    """Without the row lock, each could see 'another admin remains' and both
    succeed, leaving zero admins and nobody able to administer the system."""
    from app import crud
    from app.models import User, UserRole
    from tests.conftest import TestSessionLocal
    a = _user(db_session, "a@x.test", role="admin")
    b = _user(db_session, "b@x.test", role="admin")
    a_id, b_id = a.id, b.id

    def demote(target, actor):
        s = TestSessionLocal()
        try:
            return crud.set_user_role(s, target, "agent", actor_user_id=actor).role.value
        finally:
            s.close()

    results = _race(lambda: demote(b_id, a_id), lambda: demote(a_id, b_id))
    assert sorted(r[0] for r in results) == ["err", "ok"], results
    assert [r[1] for r in results if r[0] == "err"] == ["AdminActionRefused"]
    db_session.expire_all()
    assert db_session.query(User).filter(User.role == UserRole.admin, User.is_active == True).count() == 1  # noqa: E712


@postgres_only
def test_same_invite_submitted_twice_at_once_creates_exactly_one_account(db_session):
    from app import crud
    from app.models import User
    from tests.conftest import TestSessionLocal
    token = _invite(db_session, "race@x.test")

    def accept(name):
        s = TestSessionLocal()
        try:
            return crud.accept_invite(s, token, name, PW).name
        finally:
            s.close()

    results = _race(lambda: accept("First"), lambda: accept("Second"))
    assert sorted(r[0] for r in results) == ["err", "ok"], results
    assert [r[1] for r in results if r[0] == "err"] == ["InviteUnavailable"]  # clean refusal, not IntegrityError
    db_session.expire_all()
    assert db_session.query(User).filter(User.email == "race@x.test").count() == 1
