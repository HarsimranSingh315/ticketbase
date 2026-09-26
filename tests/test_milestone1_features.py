"""
Tests for the rest of Milestone 1: customer/contact records, ticket
assignment, the actor-attributed audit trail, and optimistic
concurrency (version conflicts).

Everything here was first verified manually (both via SQLite and via a
real PostgreSQL 16 server - see docs/current-state.md) before being
written as permanent pytest coverage, same discipline as the earlier
agent-auth work.
"""
import re

from tests.conftest import TEST_ADMIN_EMAIL, TEST_ADMIN_PASSWORD, get_csrf_token


def _create_customer(admin_client, name="Acme Corp"):
    csrf = get_csrf_token(admin_client, "/customers")
    resp = admin_client.post("/customers", data={"name": name, "csrf_token": csrf}, follow_redirects=False)
    assert resp.status_code == 303
    return int(resp.headers["location"].rstrip("/").split("/")[-1])


# --- Customers & contacts ---

def test_create_and_find_customer(admin_client):
    customer_id = _create_customer(admin_client)
    resp = admin_client.get(f"/customers/{customer_id}")
    assert resp.status_code == 200
    assert "Acme Corp" in resp.text


def test_customer_search_filters_by_name(admin_client):
    _create_customer(admin_client, name="Acme Corp")
    _create_customer(admin_client, name="Globex Inc")

    matches = admin_client.get("/customers", params={"q": "Acme"})
    assert "Acme Corp" in matches.text
    assert "Globex Inc" not in matches.text


def test_add_contact_to_customer(admin_client):
    customer_id = _create_customer(admin_client)
    csrf = get_csrf_token(admin_client, f"/customers/{customer_id}")
    resp = admin_client.post(
        f"/customers/{customer_id}/contacts",
        data={"name": "Jane Doe", "email": "jane@acme.com", "phone": "+1 (555) 123-4567", "csrf_token": csrf},
        follow_redirects=False,
    )
    assert resp.status_code == 303

    detail = admin_client.get(f"/customers/{customer_id}")
    assert "Jane Doe" in detail.text
    assert "jane@acme.com" in detail.text


def test_phone_normalization_matches_differently_formatted_numbers(db_session):
    from app import crud
    customer = crud.create_customer(db_session, name="Acme Corp")
    crud.create_contact(db_session, customer_id=customer.id, name="Jane", phone="+1 (555) 123-4567")

    # A differently-formatted version of the same number should still match.
    found = crud.find_contacts_by_phone(db_session, "555-123-4567")
    assert len(found) == 1
    assert found[0].name == "Jane"


def test_find_contacts_by_phone_returns_empty_for_no_match(db_session):
    from app import crud
    assert crud.find_contacts_by_phone(db_session, "000-000-0000") == []


# --- Linking a ticket to a customer (exact history, not semantic) ---

def test_link_ticket_to_customer_and_see_it_in_exact_history(admin_client):
    customer_id = _create_customer(admin_client)
    ticket = admin_client.post("/tickets", json={"description": "Cannot log in to the portal"}).json()

    csrf = get_csrf_token(admin_client, f"/ui/tickets/{ticket['id']}")
    resp = admin_client.post(
        f"/ui/tickets/{ticket['id']}/customer",
        data={"customer_id": customer_id, "version": ticket["version"], "csrf_token": csrf},
        follow_redirects=False,
    )
    assert resp.status_code == 303

    updated = admin_client.get(f"/tickets/{ticket['id']}").json()
    assert updated["customer_id"] == customer_id

    customer_page = admin_client.get(f"/customers/{customer_id}")
    assert f"/ui/tickets/{ticket['id']}" in customer_page.text


def test_exact_customer_history_never_shows_another_customers_ticket(admin_client):
    """The property the brief specifically warned about: a similar-
    sounding ticket from a DIFFERENT customer must never appear as if
    it were this customer's history."""
    customer_a = _create_customer(admin_client, name="Acme Corp")
    customer_b = _create_customer(admin_client, name="Globex Inc")

    ticket_a = admin_client.post("/tickets", json={"description": "VPN connection issue for Acme"}).json()
    csrf = get_csrf_token(admin_client, f"/ui/tickets/{ticket_a['id']}")
    admin_client.post(
        f"/ui/tickets/{ticket_a['id']}/customer",
        data={"customer_id": customer_a, "version": ticket_a["version"], "csrf_token": csrf},
    )

    # customer_b has no tickets linked - its history must be empty,
    # even though ticket_a's description is similar to what a Globex
    # ticket might say.
    globex_page = admin_client.get(f"/customers/{customer_b}")
    assert f"/ui/tickets/{ticket_a['id']}" not in globex_page.text


# --- Assignment ---

def test_assign_ticket_to_an_agent(admin_client, db_session):
    from app import crud
    from app.models import UserRole
    agent = crud.create_user(db_session, email="agent2@example.com", name="Agent Two", password="password123", role=UserRole.agent)

    ticket = admin_client.post("/tickets", json={"description": "test"}).json()
    csrf = get_csrf_token(admin_client, f"/ui/tickets/{ticket['id']}")
    resp = admin_client.post(
        f"/ui/tickets/{ticket['id']}/assign",
        data={"assignee_id": agent.id, "version": ticket["version"], "csrf_token": csrf},
        follow_redirects=False,
    )
    assert resp.status_code == 303

    updated = admin_client.get(f"/tickets/{ticket['id']}").json()
    assert updated["assignee_id"] == agent.id

    detail_page = admin_client.get(f"/ui/tickets/{ticket['id']}")
    assert "Agent Two" in detail_page.text


def test_unassign_ticket(admin_client, db_session):
    from app import crud
    from app.models import UserRole
    agent = crud.create_user(db_session, email="agent3@example.com", name="Agent Three", password="password123", role=UserRole.agent)

    ticket = admin_client.post("/tickets", json={"description": "test"}).json()
    csrf = get_csrf_token(admin_client, f"/ui/tickets/{ticket['id']}")
    admin_client.post(f"/ui/tickets/{ticket['id']}/assign", data={"assignee_id": agent.id, "version": ticket["version"], "csrf_token": csrf})

    csrf2 = get_csrf_token(admin_client, f"/ui/tickets/{ticket['id']}")
    resp = admin_client.post(
        f"/ui/tickets/{ticket['id']}/assign",
        data={"assignee_id": "", "version": ticket["version"] + 1, "csrf_token": csrf2},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    updated = admin_client.get(f"/tickets/{ticket['id']}").json()
    assert updated["assignee_id"] is None


# --- Audit trail ---

def test_category_confirmation_creates_an_audit_event_with_real_actor(admin_client):
    ticket = admin_client.post("/tickets", json={"description": "test"}).json()
    csrf = get_csrf_token(admin_client, f"/ui/tickets/{ticket['id']}")
    admin_client.post(
        f"/ui/tickets/{ticket['id']}/category",
        data={"category": "hardware", "version": ticket["version"], "csrf_token": csrf},
    )

    detail = admin_client.get(f"/ui/tickets/{ticket['id']}")
    assert "category confirmed" in detail.text
    assert "Test Admin" in detail.text  # the real logged-in actor's name, not a boolean


def test_status_change_creates_an_audit_event(admin_client):
    ticket = admin_client.post("/tickets", json={"description": "test"}).json()
    csrf = get_csrf_token(admin_client, f"/ui/tickets/{ticket['id']}")
    admin_client.post(
        f"/ui/tickets/{ticket['id']}/status",
        data={"status": "in_progress", "version": ticket["version"], "csrf_token": csrf},
    )
    detail = admin_client.get(f"/ui/tickets/{ticket['id']}")
    assert "status changed" in detail.text


def test_json_api_write_creates_audit_event_with_no_actor(client, db_session):
    """A write via the shared API key (no session) has no real human
    identity behind it - the audit event should record that honestly
    (actor_user_id=None) rather than fabricate one."""
    from app import crud

    ticket = client.post("/tickets", json={"description": "test"}).json()
    client.patch(f"/tickets/{ticket['id']}/category", json={"category": "billing", "version": ticket["version"]})

    events = crud.get_audit_events_for_ticket(db_session, ticket["id"])
    assert len(events) == 1
    assert events[0].actor_user_id is None
    assert events[0].action == "ticket.category_confirmed"


# --- Optimistic concurrency ---

def test_json_api_stale_write_rejected_with_409(client, db_session):
    ticket = client.post("/tickets", json={"description": "test"}).json()
    # First write succeeds and bumps the version.
    first = client.patch(f"/tickets/{ticket['id']}/status", json={"status": "in_progress", "version": ticket["version"]})
    assert first.status_code == 200

    # Retrying with the ORIGINAL (now stale) version must be rejected.
    stale = client.patch(f"/tickets/{ticket['id']}/status", json={"status": "resolved", "version": ticket["version"]})
    assert stale.status_code == 409

    # The first write's result must stand, not be overwritten - checked
    # directly against the DB since GET /tickets/{id} now requires auth.
    from app import crud
    current = crud.get_ticket(db_session, ticket["id"])
    assert current.status.value == "in_progress"


def test_ui_stale_write_rejected_and_shows_current_state(admin_client):
    ticket = admin_client.post("/tickets", json={"description": "test"}).json()
    csrf = get_csrf_token(admin_client, f"/ui/tickets/{ticket['id']}")

    # First write succeeds.
    admin_client.post(f"/ui/tickets/{ticket['id']}/status", data={"status": "in_progress", "version": ticket["version"], "csrf_token": csrf})

    # Retry with the stale version - must be rejected with a clear
    # conflict, not silently applied.
    csrf2 = get_csrf_token(admin_client, f"/ui/tickets/{ticket['id']}")
    stale_resp = admin_client.post(
        f"/ui/tickets/{ticket['id']}/status",
        data={"status": "resolved", "version": ticket["version"], "csrf_token": csrf2},
    )
    assert stale_resp.status_code == 409
    assert "changed since you loaded" in stale_resp.text

    current = admin_client.get(f"/tickets/{ticket['id']}").json()
    assert current["status"] == "in_progress"


def test_version_increments_on_every_write(admin_client):
    ticket = admin_client.post("/tickets", json={"description": "test"}).json()
    assert ticket["version"] == 1

    csrf = get_csrf_token(admin_client, f"/ui/tickets/{ticket['id']}")
    admin_client.post(f"/ui/tickets/{ticket['id']}/status", data={"status": "in_progress", "version": 1, "csrf_token": csrf})
    after_status = admin_client.get(f"/tickets/{ticket['id']}").json()
    assert after_status["version"] == 2

    csrf2 = get_csrf_token(admin_client, f"/ui/tickets/{ticket['id']}")
    admin_client.post(f"/ui/tickets/{ticket['id']}/category", data={"category": "hardware", "version": 2, "csrf_token": csrf2})
    after_category = admin_client.get(f"/tickets/{ticket['id']}").json()
    assert after_category["version"] == 3


import pytest as _pytest


@_pytest.mark.parametrize("bad_name", ["   ", "\t\n", "x" * 201])
def test_b5_customer_name_rejected_when_blank_or_too_long(admin_client, db_session, bad_name):
    from tests.conftest import get_csrf_token
    from app.models import Customer
    csrf = get_csrf_token(admin_client, "/customers")
    r = admin_client.post("/customers", data={"name": bad_name, "csrf_token": csrf}, follow_redirects=False)
    assert r.status_code == 422
    assert db_session.query(Customer).count() == 0


@_pytest.mark.parametrize("field,value", [
    ("recipient_email", "not-an-email"),
    ("recipient_email", "a@b"),
    ("subject", "   "),
    ("subject", "line1\nline2"),
    ("body", "  \n  "),
])
def test_b5_malformed_draft_rejected_and_not_stored(admin_client, db_session, field, value):
    from tests.conftest import get_csrf_token
    from app.models import OutboundMessage
    t = admin_client.post("/tickets", json={"description": "b5"}).json()
    data = {"recipient_email": "ok@example.com", "subject": "Hello", "body": "Body", field: value}
    data["csrf_token"] = get_csrf_token(admin_client, f"/ui/tickets/{t['id']}")
    r = admin_client.post(f"/ui/tickets/{t['id']}/messages", data=data, follow_redirects=False)
    assert r.status_code == 422
    assert "Draft not saved" in r.text
    assert db_session.query(OutboundMessage).count() == 0


def test_b5_valid_draft_is_normalized(admin_client, db_session):
    from tests.conftest import get_csrf_token
    from app.models import OutboundMessage
    t = admin_client.post("/tickets", json={"description": "b5 ok"}).json()
    csrf = get_csrf_token(admin_client, f"/ui/tickets/{t['id']}")
    r = admin_client.post(f"/ui/tickets/{t['id']}/messages", data={
        "recipient_email": "  Customer@Example.COM ", "subject": "  Hi  ", "body": " Body ", "csrf_token": csrf,
    }, follow_redirects=False)
    assert r.status_code == 303
    m = db_session.query(OutboundMessage).one()
    assert (m.recipient_email, m.subject, m.body) == ("customer@example.com", "Hi", "Body")


def test_b9_ticket_list_renders_pagination_and_reaches_every_record(admin_client, db_session):
    """B9: the route computed a pager but no template rendered it, so only
    the first page of tickets was reachable from the UI."""
    import re
    from app import crud
    for i in range(45):
        crud.create_ticket(db_session, f"pagination ticket {i:02d}")
    first = admin_client.get("/")
    assert "of 45" in first.text and 'rel="next"' in first.text
    seen, url = set(), "/"
    while url:
        page = admin_client.get(url)
        seen.update(re.findall(r"pagination ticket (\d\d)", page.text))
        m = re.search(r'href="([^"]+)" rel="next"', page.text)
        url = m.group(1).replace("&amp;", "&") if m else None
    assert len(seen) == 45  # every record reachable by following Next


def test_b9_pagination_preserves_search_filter(admin_client, db_session):
    import re
    from app import crud
    for i in range(25):
        crud.create_ticket(db_session, f"printer jam {i}")
    crud.create_ticket(db_session, "unrelated vpn issue")
    page = admin_client.get("/", params={"q": "printer"})
    nxt = re.search(r'href="([^"]+)" rel="next"', page.text).group(1).replace("&amp;", "&")
    assert "q=printer" in nxt and "page=2" in nxt


def test_b9_customer_history_is_paginated_not_capped(admin_client, db_session):
    from app import crud
    c = crud.create_customer(db_session, "Big Customer")
    for i in range(55):
        t = crud.create_ticket(db_session, f"history {i}")
        t.customer_id = c.id
    db_session.commit()
    r = admin_client.get(f"/customers/{c.id}")
    assert "of 55" in r.text  # the old silent cap was 50


def _assign(client, db_session, ticket_id, value):
    from app import crud
    from tests.conftest import get_csrf_token
    db_session.expire_all()
    v = crud.get_ticket(db_session, ticket_id).version
    csrf = get_csrf_token(client, f"/ui/tickets/{ticket_id}")
    return client.post(f"/ui/tickets/{ticket_id}/assign", data={"assignee_id": value, "version": v, "csrf_token": csrf}, follow_redirects=False)


def test_assignment_rejects_invalid_targets_and_stores_nothing(admin_client, db_session):
    """Each case reproduced before the fix: 'abc' raised ValueError (500);
    99999, a reviewer and a deactivated agent were all stored."""
    from app import crud
    from app.models import UserRole
    t = admin_client.post("/tickets", json={"description": "assign"}).json()
    rev = crud.create_user(db_session, email="rev@x.test", name="Rev", password="pw-12345678", role=UserRole.reviewer)
    gone = crud.create_user(db_session, email="gone@x.test", name="Gone", password="pw-12345678", role=UserRole.agent)
    gone.is_active = False
    db_session.commit()
    for value in ["abc", "99999", str(rev.id), str(gone.id)]:
        r = _assign(admin_client, db_session, t["id"], value)
        assert r.status_code == 422, value
        db_session.expire_all()
        assert crud.get_ticket(db_session, t["id"]).assignee_id is None, value


def test_assignment_to_active_agent_and_unassign_still_work(admin_client, db_session):
    from app import crud
    from app.models import UserRole
    t = admin_client.post("/tickets", json={"description": "assign ok"}).json()
    agent = crud.create_user(db_session, email="ok@x.test", name="Ok", password="pw-12345678", role=UserRole.agent)
    assert _assign(admin_client, db_session, t["id"], str(agent.id)).status_code == 303
    db_session.expire_all()
    assert crud.get_ticket(db_session, t["id"]).assignee_id == agent.id
    assert _assign(admin_client, db_session, t["id"], "").status_code == 303
    db_session.expire_all()
    assert crud.get_ticket(db_session, t["id"]).assignee_id is None


def test_my_tickets_and_unassigned_queues(admin_client, db_session):
    from app import crud
    from app.models import User
    me = db_session.query(User).filter(User.email == "test-admin@example.com").one()
    mine = crud.create_ticket(db_session, "QUEUE-mine ticket")
    crud.create_ticket(db_session, "QUEUE-unassigned ticket")
    mine.assignee_id = me.id
    db_session.commit()
    mine_page = admin_client.get("/", params={"owner": "mine"}).text
    assert "QUEUE-mine" in mine_page and "QUEUE-unassigned" not in mine_page
    un_page = admin_client.get("/", params={"owner": "unassigned"}).text
    assert "QUEUE-unassigned" in un_page and "QUEUE-mine" not in un_page
    assert 'aria-current="page"' in un_page


def test_ownership_filter_survives_status_tabs_and_search(admin_client):
    page = admin_client.get("/", params={"owner": "mine"}).text
    assert "status=open" in page and "owner=mine" in page
    assert '<input type="hidden" name="owner" value="mine">' in page
