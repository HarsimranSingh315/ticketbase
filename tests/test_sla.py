"""
Tests for SLA timers and escalation: deadline computation, the
"needs attention" view, and sla_check.py's breach detection.

Everything here was first verified manually end-to-end - including
running sla_check.py as a genuine separate subprocess (not a function
call inside the test process) and confirming a second run is correctly
idempotent - before being written as permanent tests. See
docs/current-state.md.
"""
from datetime import timedelta

from tests.conftest import get_csrf_token

SLA_HOURS = {"high": 4.0, "medium": 24.0, "low": 72.0}


def _backdate_ticket(db_session, ticket_id, hours_ago):
    from app import crud
    from app.crud import _utcnow
    ticket = crud.get_ticket(db_session, ticket_id)
    ticket.created_at = _utcnow() - timedelta(hours=hours_ago)
    db_session.commit()
    return ticket


# --- Deadline computation ---

def test_compute_sla_deadline_uses_priority_specific_hours(admin_client, db_session):
    from app import crud
    ticket = admin_client.post("/tickets", json={"description": "Server is down urgently"}).json()  # high priority
    t = crud.get_ticket(db_session, ticket["id"])
    deadline = crud.compute_sla_deadline(t, SLA_HOURS)
    expected = t.created_at + timedelta(hours=4)
    assert deadline == expected


def test_resolved_ticket_is_never_breached_regardless_of_age(admin_client, db_session):
    from app import crud
    ticket = admin_client.post("/tickets", json={"description": "Server is down urgently"}).json()
    _backdate_ticket(db_session, ticket["id"], hours_ago=1000)  # extremely overdue
    csrf = get_csrf_token(admin_client, f"/ui/tickets/{ticket['id']}")
    admin_client.post(f"/ui/tickets/{ticket['id']}/status", data={"status": "resolved", "version": ticket["version"], "csrf_token": csrf})

    t = crud.get_ticket(db_session, ticket["id"])
    assert crud.is_ticket_breached(t, SLA_HOURS) is False


def test_ticket_not_yet_breached_before_its_deadline(admin_client, db_session):
    from app import crud
    ticket = admin_client.post("/tickets", json={"description": "How do I reset my password"}).json()  # low priority, 72h
    t = _backdate_ticket(db_session, ticket["id"], hours_ago=1)  # well within 72h
    assert crud.is_ticket_breached(t, SLA_HOURS) is False


def test_ticket_breached_after_its_deadline(admin_client, db_session):
    from app import crud
    ticket = admin_client.post("/tickets", json={"description": "Server is down urgently"}).json()  # high priority, 4h
    t = _backdate_ticket(db_session, ticket["id"], hours_ago=5)  # past the 4h deadline
    assert crud.is_ticket_breached(t, SLA_HOURS) is True


def test_list_breached_tickets_sorted_most_overdue_first(admin_client, db_session):
    from app import crud
    t1 = admin_client.post("/tickets", json={"description": "Server is down urgently"}).json()
    t2 = admin_client.post("/tickets", json={"description": "System crashed urgently"}).json()
    _backdate_ticket(db_session, t1["id"], hours_ago=5)   # just past 4h deadline
    _backdate_ticket(db_session, t2["id"], hours_ago=20)  # way past 4h deadline

    breached = crud.list_breached_tickets(db_session, SLA_HOURS)
    assert [t.id for t in breached] == [t2["id"], t1["id"]]  # most overdue first


# --- Escalation / audit trail ---

def test_record_new_sla_breaches_logs_an_audit_event(admin_client, db_session):
    from app import crud
    ticket = admin_client.post("/tickets", json={"description": "Server is down urgently"}).json()
    _backdate_ticket(db_session, ticket["id"], hours_ago=5)

    recorded = crud.record_new_sla_breaches(db_session, SLA_HOURS)
    assert recorded == 1

    events = crud.get_audit_events_for_ticket(db_session, ticket["id"])
    assert any(e.action == "ticket.sla_breached" for e in events)


def test_record_new_sla_breaches_is_idempotent(admin_client, db_session):
    from app import crud
    ticket = admin_client.post("/tickets", json={"description": "Server is down urgently"}).json()
    _backdate_ticket(db_session, ticket["id"], hours_ago=5)

    first = crud.record_new_sla_breaches(db_session, SLA_HOURS)
    second = crud.record_new_sla_breaches(db_session, SLA_HOURS)
    assert first == 1
    assert second == 0  # already logged - must not re-record the same breach

    events = crud.get_audit_events_for_ticket(db_session, ticket["id"])
    breach_events = [e for e in events if e.action == "ticket.sla_breached"]
    assert len(breach_events) == 1


def test_sla_check_run_one_check_uses_the_test_database(admin_client, db_session, monkeypatch):
    """Same class of fix worker.py's tests needed: sla_check.py opens
    its own SessionLocal() (correct for a real standalone process), so
    a test calling it directly must point that at the test database."""
    import sla_check as sla_check_module
    from tests.conftest import TestSessionLocal
    monkeypatch.setattr(sla_check_module, "SessionLocal", TestSessionLocal)

    ticket = admin_client.post("/tickets", json={"description": "Server is down urgently"}).json()
    _backdate_ticket(db_session, ticket["id"], hours_ago=5)

    recorded = sla_check_module.run_one_check()
    assert recorded == 1


# --- UI ---

def test_needs_attention_tab_shows_breach_count(admin_client, db_session):
    ticket = admin_client.post("/tickets", json={"description": "Server is down urgently"}).json()
    _backdate_ticket(db_session, ticket["id"], hours_ago=5)

    home = admin_client.get("/")
    assert "Needs attention" in home.text
    assert "(1)" in home.text
    assert "Overdue" in home.text


def test_needs_attention_view_filters_to_only_breached(admin_client, db_session):
    breached_ticket = admin_client.post("/tickets", json={"description": "Server is down urgently"}).json()
    fine_ticket = admin_client.post("/tickets", json={"description": "Totally fine low priority question"}).json()
    _backdate_ticket(db_session, breached_ticket["id"], hours_ago=5)

    resp = admin_client.get("/?sla=breached")
    assert "Server is down urgently" in resp.text
    assert "Totally fine low priority question" not in resp.text


def test_needs_attention_view_empty_state(admin_client):
    resp = admin_client.get("/?sla=breached")
    assert "Nothing overdue" in resp.text


def test_ticket_detail_shows_overdue_when_breached(admin_client, db_session):
    ticket = admin_client.post("/tickets", json={"description": "Server is down urgently"}).json()
    _backdate_ticket(db_session, ticket["id"], hours_ago=5)

    detail = admin_client.get(f"/ui/tickets/{ticket['id']}")
    assert "Overdue" in detail.text
    assert "SLA deadline was" in detail.text


def test_ticket_detail_shows_sla_deadline_when_not_breached(admin_client):
    ticket = admin_client.post("/tickets", json={"description": "How do I reset my password"}).json()
    detail = admin_client.get(f"/ui/tickets/{ticket['id']}")
    assert "SLA deadline:" in detail.text
    assert "Overdue" not in detail.text


def test_ticket_detail_shows_no_sla_line_when_resolved(admin_client):
    ticket = admin_client.post("/tickets", json={"description": "test"}).json()
    csrf = get_csrf_token(admin_client, f"/ui/tickets/{ticket['id']}")
    admin_client.post(f"/ui/tickets/{ticket['id']}/status", data={"status": "resolved", "version": ticket["version"], "csrf_token": csrf})

    detail = admin_client.get(f"/ui/tickets/{ticket['id']}")
    assert "SLA deadline" not in detail.text
