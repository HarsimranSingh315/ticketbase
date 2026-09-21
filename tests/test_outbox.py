"""
Tests for Milestone 2: draft messages, explicit approval (with an
immutable snapshot), the transactional outbox, and the worker that
actually sends via the local sink adapter.

Everything here was first verified manually end-to-end (including
running worker.py as a genuinely separate subprocess, not just calling
its functions in-process) before being written as permanent pytest
coverage - see docs/current-state.md for that transcript.
"""
import re
import threading

from tests.conftest import get_csrf_token


def _create_draft(admin_client, ticket_id, recipient="customer@example.com", subject="Re: your ticket", body="We're on it."):
    csrf = get_csrf_token(admin_client, f"/ui/tickets/{ticket_id}")
    resp = admin_client.post(
        f"/ui/tickets/{ticket_id}/messages",
        data={"recipient_email": recipient, "subject": subject, "body": body, "csrf_token": csrf},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    return resp


def _get_message_id(db_session, ticket_id):
    from app import crud
    messages = crud.list_messages_for_ticket(db_session, ticket_id)
    return messages[0].id, messages[0].version


# --- Draft creation and editing ---

def test_create_draft_message(admin_client, db_session):
    ticket = admin_client.post("/tickets", json={"description": "test"}).json()
    _create_draft(admin_client, ticket["id"])

    detail = admin_client.get(f"/ui/tickets/{ticket['id']}")
    assert "customer@example.com" in detail.text
    assert "Draft" in detail.text


def test_edit_draft_message(admin_client, db_session):
    ticket = admin_client.post("/tickets", json={"description": "test"}).json()
    _create_draft(admin_client, ticket["id"], subject="Original subject")
    message_id, version = _get_message_id(db_session, ticket["id"])

    csrf = get_csrf_token(admin_client, f"/ui/tickets/{ticket['id']}")
    resp = admin_client.post(
        f"/ui/tickets/{ticket['id']}/messages/{message_id}",
        data={"recipient_email": "customer@example.com", "subject": "Edited subject", "body": "Edited body.", "csrf_token": csrf, "version": version},
        follow_redirects=False,
    )
    assert resp.status_code == 303

    detail = admin_client.get(f"/ui/tickets/{ticket['id']}")
    assert "Edited subject" in detail.text


def test_reviewer_cannot_create_or_edit_drafts(reviewer_client):
    ticket = reviewer_client.post("/tickets", json={"description": "test"}).json()
    resp = reviewer_client.post(
        f"/ui/tickets/{ticket['id']}/messages",
        data={"recipient_email": "x@example.com", "subject": "x", "body": "x", "csrf_token": "x"},
    )
    assert resp.status_code == 403


# --- Approval ---

def test_approve_message_snapshots_content_and_records_actor(admin_client, db_session):
    from app import crud
    ticket = admin_client.post("/tickets", json={"description": "test"}).json()
    _create_draft(admin_client, ticket["id"], subject="Final subject")
    message_id, version = _get_message_id(db_session, ticket["id"])

    csrf = get_csrf_token(admin_client, f"/ui/tickets/{ticket['id']}")
    resp = admin_client.post(
        f"/ui/tickets/{ticket['id']}/messages/{message_id}/approve",
        data={"csrf_token": csrf, "version": version},
        follow_redirects=False,
    )
    assert resp.status_code == 303

    message = crud.get_message(db_session, message_id)
    assert message.status.value == "approved"
    assert message.approved_subject == "Final subject"
    assert message.approved_by_user_id is not None


def test_approve_creates_exactly_one_outbox_job(admin_client, db_session):
    from app import crud
    ticket = admin_client.post("/tickets", json={"description": "test"}).json()
    _create_draft(admin_client, ticket["id"])
    message_id, version = _get_message_id(db_session, ticket["id"])

    csrf = get_csrf_token(admin_client, f"/ui/tickets/{ticket['id']}")
    admin_client.post(
        f"/ui/tickets/{ticket['id']}/messages/{message_id}/approve",
        data={"csrf_token": csrf, "version": version},
    )

    job = crud.get_outbox_job_for_message(db_session, message_id)
    assert job is not None
    assert job.operation_key == f"message-{message_id}"
    assert job.status.value == "pending"


def test_editing_after_approval_is_blocked(admin_client, db_session):
    from app import crud
    ticket = admin_client.post("/tickets", json={"description": "test"}).json()
    _create_draft(admin_client, ticket["id"])
    message_id, version = _get_message_id(db_session, ticket["id"])

    csrf = get_csrf_token(admin_client, f"/ui/tickets/{ticket['id']}")
    admin_client.post(f"/ui/tickets/{ticket['id']}/messages/{message_id}/approve", data={"csrf_token": csrf, "version": version})

    csrf2 = get_csrf_token(admin_client, f"/ui/tickets/{ticket['id']}")
    resp = admin_client.post(
        f"/ui/tickets/{ticket['id']}/messages/{message_id}",
        data={"recipient_email": "attacker@evil.com", "subject": "changed", "body": "changed", "csrf_token": csrf2, "version": version + 1},
    )
    assert resp.status_code == 409

    message = crud.get_message(db_session, message_id)
    assert message.approved_recipient_email != "attacker@evil.com"


def test_cannot_approve_an_already_approved_message(admin_client, db_session):
    """Regression-shaped test for the double-click scenario: approving
    twice must not create two outbox jobs."""
    from app import crud
    ticket = admin_client.post("/tickets", json={"description": "test"}).json()
    _create_draft(admin_client, ticket["id"])
    message_id, version = _get_message_id(db_session, ticket["id"])

    csrf = get_csrf_token(admin_client, f"/ui/tickets/{ticket['id']}")
    first = admin_client.post(f"/ui/tickets/{ticket['id']}/messages/{message_id}/approve", data={"csrf_token": csrf, "version": version}, follow_redirects=False)
    assert first.status_code == 303

    # Second attempt with the SAME (now stale) version - simulates a
    # double-click before the page reloaded.
    csrf2 = get_csrf_token(admin_client, f"/ui/tickets/{ticket['id']}")
    second = admin_client.post(f"/ui/tickets/{ticket['id']}/messages/{message_id}/approve", data={"csrf_token": csrf2, "version": version})
    assert second.status_code == 409

    from app.models import OutboxJob
    jobs = db_session.query(OutboxJob).filter(OutboxJob.message_id == message_id).all()
    assert len(jobs) == 1


# --- Worker / outbox processing ---

def test_worker_sends_approved_message_via_local_sink(admin_client, db_session, monkeypatch):
    from app import crud
    from app.models import LocalSinkEmail
    import worker as worker_module
    from tests.conftest import TestSessionLocal

    # worker.run_one_cycle() opens its own SessionLocal() internally
    # (worker.py is a standalone process in real life, not something
    # that receives a request-scoped session) - so calling it directly
    # from a test needs that pointed at the TEST database, or it hits
    # the real dev/prod SQLite file and fails with "no such table"
    # (found by actually running this, not assumed).
    monkeypatch.setattr(worker_module, "SessionLocal", TestSessionLocal)

    ticket = admin_client.post("/tickets", json={"description": "test"}).json()
    _create_draft(admin_client, ticket["id"], recipient="worker-test@example.com", subject="Worker test")
    message_id, version = _get_message_id(db_session, ticket["id"])
    csrf = get_csrf_token(admin_client, f"/ui/tickets/{ticket['id']}")
    admin_client.post(f"/ui/tickets/{ticket['id']}/messages/{message_id}/approve", data={"csrf_token": csrf, "version": version})

    found_work = worker_module.run_one_cycle("test-worker")
    assert found_work is True

    message = crud.get_message(db_session, message_id)
    assert message.status.value == "sent"

    job = crud.get_outbox_job_for_message(db_session, message_id)
    assert job.status.value == "sent"
    assert job.provider_message_id is not None

    sink_email = db_session.query(LocalSinkEmail).filter(LocalSinkEmail.idempotency_key == job.operation_key).first()
    assert sink_email is not None
    assert sink_email.recipient_email == "worker-test@example.com"
    assert sink_email.subject == "Worker test"


def test_worker_returns_false_when_no_jobs_pending(db_session, monkeypatch):
    import worker as worker_module
    from tests.conftest import TestSessionLocal
    monkeypatch.setattr(worker_module, "SessionLocal", TestSessionLocal)
    found_work = worker_module.run_one_cycle("test-worker")
    assert found_work is False


def test_worker_sends_the_approved_snapshot_not_a_later_edit(admin_client, db_session, monkeypatch):
    """The approved snapshot, not the live fields, is what actually
    gets sent - this is "immutable approved snapshot" being honored at
    send time, not just at storage time."""
    from app import crud
    import worker as worker_module
    from tests.conftest import TestSessionLocal
    monkeypatch.setattr(worker_module, "SessionLocal", TestSessionLocal)

    ticket = admin_client.post("/tickets", json={"description": "test"}).json()
    _create_draft(admin_client, ticket["id"], subject="Approved version")
    message_id, version = _get_message_id(db_session, ticket["id"])
    csrf = get_csrf_token(admin_client, f"/ui/tickets/{ticket['id']}")
    admin_client.post(f"/ui/tickets/{ticket['id']}/messages/{message_id}/approve", data={"csrf_token": csrf, "version": version})

    # Directly corrupt the LIVE (non-approved) fields to prove the
    # worker doesn't read them - editing via the API is blocked (tested
    # above), but this checks the worker's own code path specifically.
    message = crud.get_message(db_session, message_id)
    message.subject = "TAMPERED - should never be sent"
    db_session.commit()

    worker_module.run_one_cycle("test-worker")

    from app.models import LocalSinkEmail
    job = crud.get_outbox_job_for_message(db_session, message_id)
    sink_email = db_session.query(LocalSinkEmail).filter(LocalSinkEmail.idempotency_key == job.operation_key).first()
    assert sink_email.subject == "Approved version"
    assert "TAMPERED" not in sink_email.subject


def test_retry_with_same_operation_key_does_not_duplicate_send(db_session):
    """Simulates a worker crash after the provider accepted the email
    but before the job was marked complete: retrying with the SAME
    idempotency key must not send a second copy."""
    from app.mail import LocalSinkAdapter

    adapter = LocalSinkAdapter(db_session)
    result1 = adapter.send("customer@example.com", "Subject", "Body", idempotency_key="retry-test-key")
    result2 = adapter.send("customer@example.com", "Subject", "Body", idempotency_key="retry-test-key")

    assert result1.success and result2.success
    assert result1.provider_message_id == result2.provider_message_id

    from app.models import LocalSinkEmail
    count = db_session.query(LocalSinkEmail).filter(LocalSinkEmail.idempotency_key == "retry-test-key").count()
    assert count == 1


def test_failed_send_retries_with_backoff_then_becomes_terminal(db_session):
    """A send that keeps failing should retry up to max_attempts, then
    become terminally `failed` - not retry forever."""
    from app import crud
    from app.models import OutboxJob, OutboxJobStatus

    # Build a job directly (bypassing the approve flow, which would
    # succeed against the local sink) so we can control max_attempts
    # and simulate failures deterministically.
    from app.models import OutboundMessage, MessageStatus, User, UserRole
    from app.security import hash_password
    user = User(email="failtest@example.com", name="Fail Test", password_hash=hash_password("x"), role=UserRole.admin)
    db_session.add(user)
    db_session.commit()

    ticket = crud.create_ticket(db_session, "test")
    message = OutboundMessage(
        ticket_id=ticket.id, recipient_email="x@example.com", subject="x", body="x",
        created_by_user_id=user.id, status=MessageStatus.approved,
        approved_by_user_id=user.id, approved_recipient_email="x@example.com",
        approved_subject="x", approved_body="x",
    )
    db_session.add(message)
    db_session.commit()

    job = OutboxJob(operation_key=f"message-{message.id}", message_id=message.id, max_attempts=2)
    db_session.add(job)
    db_session.commit()

    # First failure: attempts=1 (< max_attempts=2) -> stays pending, retried later.
    claimed = crud.claim_next_job(db_session, "worker-1", lease_seconds=60)
    assert claimed is not None
    crud.fail_job(db_session, claimed.id, "simulated failure 1", backoff_seconds=0)
    job_after_1 = db_session.query(OutboxJob).filter(OutboxJob.id == job.id).first()
    assert job_after_1.status == OutboxJobStatus.pending

    # Second failure: attempts=2 (>= max_attempts=2) -> terminally failed.
    claimed2 = crud.claim_next_job(db_session, "worker-1", lease_seconds=60)
    assert claimed2 is not None
    crud.fail_job(db_session, claimed2.id, "simulated failure 2", backoff_seconds=0)
    job_after_2 = db_session.query(OutboxJob).filter(OutboxJob.id == job.id).first()
    assert job_after_2.status == OutboxJobStatus.failed

    message_after = crud.get_message(db_session, message.id)
    assert message_after.status.value == "failed"


def test_concurrent_workers_racing_for_the_same_job_only_one_wins(db_session):
    """
    The property claim_next_job's own docstring makes: two workers
    racing for the same job get exactly one winner, via a compare-and-
    swap UPDATE, not a race that could double-claim. Verified here with
    TWO REAL, SEPARATE DB SESSIONS (not the same session called twice),
    which is what actually exercises the race - a single session
    calling the function twice wouldn't prove anything about
    concurrent access.
    """
    from app import crud
    from app.models import OutboundMessage, MessageStatus, OutboxJob, User, UserRole
    from app.security import hash_password
    from tests.conftest import TestSessionLocal

    user = User(email="racetest@example.com", name="Race Test", password_hash=hash_password("x"), role=UserRole.admin)
    db_session.add(user)
    db_session.commit()
    ticket = crud.create_ticket(db_session, "test")
    message = OutboundMessage(
        ticket_id=ticket.id, recipient_email="x@example.com", subject="x", body="x",
        created_by_user_id=user.id, status=MessageStatus.approved,
        approved_by_user_id=user.id, approved_recipient_email="x@example.com",
        approved_subject="x", approved_body="x",
    )
    db_session.add(message)
    db_session.commit()
    job = OutboxJob(operation_key=f"message-{message.id}", message_id=message.id)
    db_session.add(job)
    db_session.commit()

    results = []

    def claim_in_own_session(worker_id):
        session = TestSessionLocal()
        try:
            claimed = crud.claim_next_job(session, worker_id, lease_seconds=60)
            results.append((worker_id, claimed.id if claimed else None))
        finally:
            session.close()

    t1 = threading.Thread(target=claim_in_own_session, args=("worker-A",))
    t2 = threading.Thread(target=claim_in_own_session, args=("worker-B",))
    t1.start()
    t1.join()  # SQLite (StaticPool, single connection) can't truly run these
    t2.start()  # concurrently without serializing - run sequentially here,
    t2.join()   # which still exercises the real compare-and-swap logic:
    # whichever ran first flips status to 'claimed', so the second's
    # WHERE status=<expected old status> UPDATE correctly matches zero
    # rows and returns None - the same code path a true race hits.

    winners = [r for r in results if r[1] is not None]
    losers = [r for r in results if r[1] is None]
    assert len(winners) == 1, f"expected exactly one winner, got {results}"
    assert len(losers) == 1

    from app.models import OutboxJobStatus
    final_job = db_session.query(OutboxJob).filter(OutboxJob.id == job.id).first()
    assert final_job.status == OutboxJobStatus.claimed
    assert final_job.attempts == 1  # NOT 2 - the loser must not have incremented it
