"""
Internal notes, and the boundary that keeps them away from customers.

Each boundary test plants a unique marker in an internal note, then drives
the REAL customer-facing or external path end to end (approve -> worker ->
mail sink; suggestion -> LLM request payload; JSON API) and asserts the
marker never crosses. A structural test also fails if any customer-facing
module ever starts referencing the notes model at all.
"""
import pathlib
import re

import pytest

from tests.conftest import get_csrf_token, csrf_headers

MARKER = "INTERNAL-ONLY-7f3c9a: customer is on a payment plan, do not mention"


def _note(client, ticket_id, body=MARKER):
    csrf = get_csrf_token(client, f"/ui/tickets/{ticket_id}")
    return client.post(f"/ui/tickets/{ticket_id}/notes", data={"body": body, "csrf_token": csrf}, follow_redirects=False)


def _ticket(client, description="Printer offline on floor 3"):
    return client.post("/tickets", json={"description": description}).json()


# --- The feature ---------------------------------------------------------

def test_agent_note_appears_in_conversation_labelled_internal(agent_client):
    t = _ticket(agent_client)
    assert _note(agent_client, t["id"]).status_code == 303
    page = agent_client.get(f"/ui/tickets/{t['id']}").text
    assert MARKER in page
    assert "Internal note" in page and "never sent to the customer" in page


def test_note_audit_event_records_the_fact_not_the_text(agent_client, db_session):
    from app.models import AuditEvent
    t = _ticket(agent_client)
    _note(agent_client, t["id"])
    events = db_session.query(AuditEvent).filter(AuditEvent.action == "ticket.note_added").all()
    assert len(events) == 1
    assert "payment plan" not in (events[0].details or "")  # internal text not copied into the audit trail


def test_reviewer_cannot_add_notes(reviewer_client, db_session):
    from app import crud
    t = crud.create_ticket(db_session, "reviewer test")
    csrf = get_csrf_token(reviewer_client, f"/ui/tickets/{t.id}")
    r = reviewer_client.post(f"/ui/tickets/{t.id}/notes", data={"body": "x", "csrf_token": csrf}, follow_redirects=False)
    assert r.status_code == 403
    assert crud.list_notes_for_ticket(db_session, t.id) == []


def test_note_requires_csrf(agent_client, db_session):
    from app import crud
    t = _ticket(agent_client)
    r = agent_client.post(f"/ui/tickets/{t['id']}/notes", data={"body": "x", "csrf_token": "forged"}, follow_redirects=False)
    assert r.status_code == 403
    assert crud.list_notes_for_ticket(db_session, t["id"]) == []


def test_blank_note_rejected_and_nothing_stored(agent_client, db_session):
    from app import crud
    t = _ticket(agent_client)
    r = _note(agent_client, t["id"], body="   \n  ")
    assert r.status_code == 422
    assert crud.list_notes_for_ticket(db_session, t["id"]) == []


def test_note_on_missing_ticket_is_404(agent_client):
    csrf = get_csrf_token(agent_client, "/")
    r = agent_client.post("/ui/tickets/99999/notes", data={"body": "x", "csrf_token": csrf})
    assert r.status_code == 404


# --- The boundary ----------------------------------------------------------

def test_boundary_note_never_reaches_a_sent_email(admin_client, db_session, monkeypatch):
    """Draft, approve and actually SEND an email on a ticket with an internal
    note; neither the approved snapshot nor the delivered mail contains it."""
    import worker as worker_module
    from app import crud
    from app.models import LocalSinkEmail
    from tests.conftest import TestSessionLocal
    monkeypatch.setattr(worker_module, "SessionLocal", TestSessionLocal)

    t = _ticket(admin_client)
    _note(admin_client, t["id"])
    csrf = get_csrf_token(admin_client, f"/ui/tickets/{t['id']}")
    admin_client.post(f"/ui/tickets/{t['id']}/messages", data={
        "recipient_email": "customer@example.com", "subject": "Update on your ticket",
        "body": "We're looking into it.", "csrf_token": csrf})
    msg = crud.list_messages_for_ticket(db_session, t["id"])[0]
    csrf = get_csrf_token(admin_client, f"/ui/tickets/{t['id']}")
    admin_client.post(f"/ui/tickets/{t['id']}/messages/{msg.id}/approve", data={"csrf_token": csrf, "version": msg.version})
    assert worker_module.run_one_cycle("boundary-test") is True

    db_session.expire_all()
    sent = db_session.query(LocalSinkEmail).all()
    assert len(sent) == 1
    delivered = f"{sent[0].subject}\n{sent[0].body}"
    snapshot = crud.get_message(db_session, msg.id)
    assert "7f3c9a" not in delivered
    assert "7f3c9a" not in f"{snapshot.approved_subject}\n{snapshot.approved_body}"


def test_boundary_note_never_sent_to_external_ai_provider(admin_client, monkeypatch):
    """With an LLM configured, capture the exact outbound request: the ticket
    description may go to the provider, internal notes must not."""
    from unittest.mock import MagicMock
    from app.main import app
    from app.config import get_settings, Settings
    captured = []

    def fake_post(url, **kwargs):
        captured.append(repr(kwargs.get("json")))
        resp = MagicMock(status_code=200, content=b"{}")
        resp.json.return_value = {"choices": [{"message": {"content": "Try restarting the printer."}}]}
        return resp

    monkeypatch.setattr("app.llm.requests.post", fake_post)
    t = _ticket(admin_client, "Printer offline, printer queue stuck, printer not printing")
    _note(admin_client, t["id"])
    base = app.dependency_overrides.get(get_settings, get_settings)()
    app.dependency_overrides[get_settings] = lambda: Settings(**{**base.model_dump(), "llm_api_key": "test-key"})
    try:
        r = admin_client.post(f"/tickets/{t['id']}/suggest", headers=csrf_headers(admin_client))
    finally:
        app.dependency_overrides.pop(get_settings, None)
    assert r.status_code == 200
    if not captured:
        pytest.skip("retrieval abstained, so no provider call was made for this corpus")
    assert any("Printer offline" in payload for payload in captured), "capture must see the ticket text, or this test proves nothing"
    assert all("7f3c9a" not in payload for payload in captured)


def test_boundary_note_not_exposed_by_json_api(admin_client):
    t = _ticket(admin_client)
    _note(admin_client, t["id"])
    assert "7f3c9a" not in admin_client.get(f"/tickets/{t['id']}").text
    assert "7f3c9a" not in admin_client.get("/tickets").text


CUSTOMER_FACING_AND_EXTERNAL = ["app/mail.py", "worker.py", "app/llm.py", "app/telephony.py"]


@pytest.mark.parametrize("path", CUSTOMER_FACING_AND_EXTERNAL)
def test_boundary_structural_no_customer_facing_module_reads_notes(path):
    """If anyone later wires notes into email, calls or the AI prompt, this
    fails and forces a deliberate decision instead of a silent leak."""
    source = pathlib.Path(path).read_text()
    assert not re.search(r"TicketNote|ticket_notes|list_notes_for_ticket|ticket_conversation", source), path


def test_boundary_structural_outbox_functions_never_read_notes():
    """The outbox/send functions live in crud.py beside the notes code, so
    check those specific function bodies rather than the whole module."""
    import inspect
    from app import crud
    for fn in (crud.create_draft, crud.update_draft, crud.approve_message, crud.claim_next_job,
               crud.complete_job, crud.fail_job):
        src = inspect.getsource(fn)
        assert "TicketNote" not in src and "list_notes_for_ticket" not in src, fn.__name__
