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
