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
import pytest

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
    crud.fail_job(db_session, claimed.id, claimed.lease_token, "simulated failure 1", backoff_seconds=0)
    job_after_1 = db_session.query(OutboxJob).filter(OutboxJob.id == job.id).first()
    assert job_after_1.status == OutboxJobStatus.pending

    # Second failure: attempts=2 (>= max_attempts=2) -> terminally failed.
    claimed2 = crud.claim_next_job(db_session, "worker-1", lease_seconds=60)
    assert claimed2 is not None
    crud.fail_job(db_session, claimed2.id, claimed2.lease_token, "simulated failure 2", backoff_seconds=0)
    job_after_2 = db_session.query(OutboxJob).filter(OutboxJob.id == job.id).first()
    assert job_after_2.status == OutboxJobStatus.failed

    message_after = crud.get_message(db_session, message.id)
    assert message_after.status.value == "failed"


@pytest.mark.skipif(
    __import__("tests.conftest", fromlist=["engine"]).engine.dialect.name != "postgresql",
    reason="Genuine thread race: SQLite's single shared StaticPool connection makes it fail ~3% of runs "
           "(measured 2/60) for reasons unrelated to the code under test. Runs on PostgreSQL in CI.",
)
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

    # A real threading.Barrier forces both threads to actually reach
    # their claim attempt at the same instant, rather than the
    # misleading version this test used to have: t1.start(); t1.join()
    # BEFORE t2 even started, which is fully sequential regardless of
    # using two Thread objects - flagged correctly by an external
    # review, since it exercises the right CODE PATH but proves nothing
    # about actual concurrent access. On SQLite (StaticPool, one shared
    # connection) the database operations still serialize even with the
    # barrier, but the two claim attempts now genuinely interleave at
    # the point they're issued - and on real Postgres (see
    # docs/current-state.md's verification with a real barrier and two
    # separate connections), this is a genuine, no-caveats race.
    barrier = threading.Barrier(2)

    def claim_in_own_session(worker_id):
        session = TestSessionLocal()
        try:
            barrier.wait()
            claimed = crud.claim_next_job(session, worker_id, lease_seconds=60)
            results.append((worker_id, claimed.id if claimed else None))
        finally:
            session.close()

    t1 = threading.Thread(target=claim_in_own_session, args=("worker-A",))
    t2 = threading.Thread(target=claim_in_own_session, args=("worker-B",))
    t1.start()
    t2.start()
    t1.join()
    t2.join()

    winners = [r for r in results if r[1] is not None]
    losers = [r for r in results if r[1] is None]
    assert len(winners) == 1, f"expected exactly one winner, got {results}"
    assert len(losers) == 1

    from app.models import OutboxJobStatus
    final_job = db_session.query(OutboxJob).filter(OutboxJob.id == job.id).first()
    assert final_job.status == OutboxJobStatus.claimed
    assert final_job.attempts == 1  # NOT 2 - the loser must not have incremented it


def test_stale_completion_after_lease_reclaim_is_safely_ignored(db_session):
    """
    The exact scenario an external review named: "stale workers can
    overwrite results." Simulates a worker whose lease expired mid-send
    (network was just slow) - another worker reclaims and completes the
    job first. When the ORIGINAL worker's slow completion finally
    arrives, using its now-stale lease_token, it must be ignored, not
    allowed to un-sent a message that's already correctly marked sent.
    """
    from app import crud
    from app.models import OutboundMessage, MessageStatus, OutboxJob, OutboxJobStatus, User, UserRole
    from app.security import hash_password

    user = User(email="stale1@example.com", name="Stale1", password_hash=hash_password("x"), role=UserRole.admin)
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

    # Worker A claims it (its lease will be this token).
    claimed_by_a = crud.claim_next_job(db_session, "worker-A", lease_seconds=60)
    stale_token = claimed_by_a.lease_token

    # Simulate A's lease expiring (backdate it) before A ever calls
    # complete_job - e.g. a slow network call that outlives the lease.
    claimed_by_a.leased_until = crud._utcnow() - __import__("datetime").timedelta(seconds=1)
    db_session.commit()

    # Worker B reclaims the expired lease and completes it correctly.
    claimed_by_b = crud.claim_next_job(db_session, "worker-B", lease_seconds=60)
    assert claimed_by_b is not None
    assert claimed_by_b.lease_token != stale_token
    crud.complete_job(db_session, claimed_by_b.id, claimed_by_b.lease_token, "real-provider-id-from-b")

    # Now A's slow completion finally arrives, with its STALE token.
    crud.complete_job(db_session, job.id, stale_token, "stale-provider-id-from-a")

    # B's result must stand - A's stale completion must not have overwritten it.
    final = db_session.query(OutboxJob).filter(OutboxJob.id == job.id).first()
    assert final.status == OutboxJobStatus.sent
    assert final.provider_message_id == "real-provider-id-from-b"


def test_stale_failure_after_lease_reclaim_is_safely_ignored(db_session):
    """The mirror case: a stale FAILURE report must not revert a job
    another worker has already completed."""
    from app import crud
    from app.models import OutboundMessage, MessageStatus, OutboxJob, OutboxJobStatus, User, UserRole
    from app.security import hash_password

    user = User(email="stale2@example.com", name="Stale2", password_hash=hash_password("x"), role=UserRole.admin)
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

    claimed_by_a = crud.claim_next_job(db_session, "worker-A", lease_seconds=60)
    stale_token = claimed_by_a.lease_token
    claimed_by_a.leased_until = crud._utcnow() - __import__("datetime").timedelta(seconds=1)
    db_session.commit()

    claimed_by_b = crud.claim_next_job(db_session, "worker-B", lease_seconds=60)
    crud.complete_job(db_session, claimed_by_b.id, claimed_by_b.lease_token, "real-provider-id")

    # A's stale FAILURE report arrives after B already succeeded.
    crud.fail_job(db_session, job.id, stale_token, "stale failure from A", backoff_seconds=0)

    final = db_session.query(OutboxJob).filter(OutboxJob.id == job.id).first()
    assert final.status == OutboxJobStatus.sent  # must NOT have been reverted to pending/failed
    assert final.provider_message_id == "real-provider-id"


def test_repeated_crashes_cannot_reclaim_past_max_attempts(db_session):
    """
    Before this fix, the attempt ceiling was only enforced inside
    fail_job - a worker that crashed BEFORE reaching fail_job (the
    realistic crash case) left the job re-claimable forever, each
    reclaim incrementing attempts with no upper bound actually
    enforced. Simulates three crashes in a row on a job with
    max_attempts=2: the job must end up terminally failed, not
    endlessly reclaimable.
    """
    from app import crud
    from app.models import OutboundMessage, MessageStatus, OutboxJob, OutboxJobStatus, User, UserRole
    from app.security import hash_password
    import datetime

    user = User(email="crash1@example.com", name="Crash1", password_hash=hash_password("x"), role=UserRole.admin)
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

    # Crash 1: claim, then simulate a crash (never calls complete/fail_job) - just expire the lease.
    claimed1 = crud.claim_next_job(db_session, "worker-1", lease_seconds=60)
    assert claimed1.attempts == 1
    claimed1.leased_until = crud._utcnow() - datetime.timedelta(seconds=1)
    db_session.commit()

    # Crash 2: another worker reclaims (attempts now 2 == max_attempts), also "crashes".
    claimed2 = crud.claim_next_job(db_session, "worker-2", lease_seconds=60)
    assert claimed2 is not None
    assert claimed2.attempts == 2
    claimed2.leased_until = crud._utcnow() - datetime.timedelta(seconds=1)
    db_session.commit()

    # A third claim attempt must NOT reclaim it - it's now at its ceiling.
    # claim_next_job's own upfront pass should have already terminally
    # failed it instead.
    claimed3 = crud.claim_next_job(db_session, "worker-3", lease_seconds=60)
    assert claimed3 is None or claimed3.id != job.id

    final = db_session.query(OutboxJob).filter(OutboxJob.id == job.id).first()
    assert final.status == OutboxJobStatus.failed
    assert final.attempts == 2  # not incremented a third time

    final_message = crud.get_message(db_session, message.id)
    assert final_message.status.value == "failed"


def test_approve_message_propagates_configured_max_attempts(db_session):
    """Before this fix, approve_message always used the OutboxJob
    model's default (5), ignoring whatever OUTBOX_MAX_ATTEMPTS was
    actually configured to. Verifies the setting genuinely reaches the
    created row now."""
    from app import crud
    from app.models import OutboundMessage, MessageStatus, OutboxJob, User, UserRole
    from app.security import hash_password

    user = User(email="maxattempts@example.com", name="MaxAttempts", password_hash=hash_password("x"), role=UserRole.admin)
    db_session.add(user)
    db_session.commit()
    ticket = crud.create_ticket(db_session, "test")
    message = OutboundMessage(
        ticket_id=ticket.id, recipient_email="x@example.com", subject="x", body="x",
        created_by_user_id=user.id, status=MessageStatus.draft,
    )
    db_session.add(message)
    db_session.commit()

    crud.approve_message(db_session, message.id, actor_user_id=user.id, expected_version=message.version, max_attempts=9)

    job = db_session.query(OutboxJob).filter(OutboxJob.message_id == message.id).first()
    assert job.max_attempts == 9


def test_editing_a_message_through_the_wrong_ticket_url_is_rejected(admin_client, db_session):
    """
    A real relationship-integrity gap an external review found: the
    route took both ticket_id and message_id from the URL, but the
    underlying crud function only ever looked up the message by
    message_id - a message could be edited through a completely
    different ticket's URL (typo, stale tab, guessed ID) and it would
    silently work.
    """
    from tests.conftest import get_csrf_token
    ticket_a = admin_client.post("/tickets", json={"description": "ticket A"}).json()
    ticket_b = admin_client.post("/tickets", json={"description": "ticket B"}).json()

    csrf = get_csrf_token(admin_client, f"/ui/tickets/{ticket_a['id']}")
    admin_client.post(
        f"/ui/tickets/{ticket_a['id']}/messages",
        data={"recipient_email": "x@example.com", "subject": "x", "body": "x", "csrf_token": csrf},
    )
    from app import crud
    message = crud.list_messages_for_ticket(db_session, ticket_a["id"])[0]
    message_id, message_version = message.id, message.version

    # Try to edit ticket A's real message, but through ticket B's URL.
    csrf2 = get_csrf_token(admin_client, f"/ui/tickets/{ticket_b['id']}")
    resp = admin_client.post(
        f"/ui/tickets/{ticket_b['id']}/messages/{message_id}",
        data={"recipient_email": "attacker@evil.com", "subject": "changed", "body": "changed", "version": message_version, "csrf_token": csrf2},
    )
    assert resp.status_code == 404

    # And confirm it genuinely wasn't touched.
    db_session.expire_all()
    unchanged = crud.get_message(db_session, message_id)
    assert unchanged.subject == "x"


def test_approving_a_message_through_the_wrong_ticket_url_is_rejected(admin_client, db_session):
    """Same relationship check, for approval - arguably more important
    here, since approving queues a real send."""
    from tests.conftest import get_csrf_token
    ticket_a = admin_client.post("/tickets", json={"description": "ticket A"}).json()
    ticket_b = admin_client.post("/tickets", json={"description": "ticket B"}).json()

    csrf = get_csrf_token(admin_client, f"/ui/tickets/{ticket_a['id']}")
    admin_client.post(
        f"/ui/tickets/{ticket_a['id']}/messages",
        data={"recipient_email": "x@example.com", "subject": "x", "body": "x", "csrf_token": csrf},
    )
    from app import crud
    message = crud.list_messages_for_ticket(db_session, ticket_a["id"])[0]
    message_id, message_version = message.id, message.version

    csrf2 = get_csrf_token(admin_client, f"/ui/tickets/{ticket_b['id']}")
    resp = admin_client.post(
        f"/ui/tickets/{ticket_b['id']}/messages/{message_id}/approve",
        data={"version": message_version, "csrf_token": csrf2},
    )
    assert resp.status_code == 404

    db_session.expire_all()
    unchanged = crud.get_message(db_session, message_id)
    assert unchanged.status.value == "draft"  # never approved, no job ever queued


def _approved_job(db_session, email="b2@example.com", max_attempts=1):
    from app import crud
    from app.models import OutboundMessage, MessageStatus, OutboxJob, User, UserRole
    from app.security import hash_password
    user = User(email=email, name="B2", password_hash=hash_password("x"), role=UserRole.admin)
    db_session.add(user); db_session.commit()
    ticket = crud.create_ticket(db_session, "b2")
    msg = OutboundMessage(ticket_id=ticket.id, recipient_email="x@example.com", subject="x", body="x",
                          created_by_user_id=user.id, status=MessageStatus.approved, approved_by_user_id=user.id,
                          approved_recipient_email="x@example.com", approved_subject="x", approved_body="x")
    db_session.add(msg); db_session.commit()
    job = OutboxJob(operation_key=f"message-{msg.id}", message_id=msg.id, max_attempts=max_attempts)
    db_session.add(job); db_session.commit()
    return msg, job


def test_b2_completion_between_cleanup_select_and_update_is_not_overwritten(db_session):
    """
    Deterministic reproduction of B2's interleaving:
      1. cleanup observes an exhausted, expired job (captures its lease)
      2. the owning worker's slow send succeeds -> complete_job -> sent
      3. cleanup's UPDATE runs with the snapshot from step 1
    Before the fix, step 3 overwrote the confirmed send as failed.
    """
    import datetime
    from app import crud
    from app.models import OutboxJob, OutboxJobStatus, MessageStatus
    msg, job = _approved_job(db_session)
    claimed = crud.claim_next_job(db_session, "w1", lease_seconds=60)
    token = claimed.lease_token
    claimed.leased_until = crud._utcnow() - datetime.timedelta(seconds=1)
    db_session.commit()

    observed = (job.id, msg.id, token)                                          # step 1
    crud.complete_job(db_session, job.id, token, "provider-accepted-id")        # step 2
    transitioned = crud.fail_exhausted_job(db_session, *observed, crud._utcnow())  # step 3

    assert transitioned is False
    db_session.expire_all()
    final = db_session.query(OutboxJob).get(job.id)
    assert final.status == OutboxJobStatus.sent
    assert final.provider_message_id == "provider-accepted-id"
    assert crud.get_message(db_session, msg.id).status == MessageStatus.sent


def test_b2_genuinely_stuck_job_is_still_failed_with_message(db_session):
    """The fix must not disable the cleanup itself."""
    import datetime
    from app import crud
    from app.models import OutboxJob, OutboxJobStatus, MessageStatus
    msg, job = _approved_job(db_session, email="b2b@example.com")
    claimed = crud.claim_next_job(db_session, "w1", lease_seconds=60)
    claimed.leased_until = crud._utcnow() - datetime.timedelta(seconds=1)
    db_session.commit()
    crud.claim_next_job(db_session, "w2", lease_seconds=60)  # triggers the cleanup pass
    db_session.expire_all()
    assert db_session.query(OutboxJob).get(job.id).status == OutboxJobStatus.failed
    assert crud.get_message(db_session, msg.id).status == MessageStatus.failed


def _draft_on_ticket(admin_client, db_session):
    from tests.conftest import get_csrf_token
    from app import crud
    t = admin_client.post("/tickets", json={"description": "b11"}).json()
    csrf = get_csrf_token(admin_client, f"/ui/tickets/{t['id']}")
    admin_client.post(f"/ui/tickets/{t['id']}/messages", data={
        "recipient_email": "c@example.com", "subject": "Original", "body": "Original body", "csrf_token": csrf})
    return t, crud.list_messages_for_ticket(db_session, t["id"])[0]


def test_b11_conflict_preserves_the_agents_unsaved_text(admin_client, db_session):
    """B11: a stale-version save used to re-render with only the database
    copy - everything the agent had typed was lost."""
    from tests.conftest import get_csrf_token
    from app import crud
    t, m = _draft_on_ticket(admin_client, db_session)
    stale_version = m.version
    crud.update_draft(db_session, m.id, "c@example.com", "Colleague's subject", "Colleague's body", expected_version=m.version)

    csrf = get_csrf_token(admin_client, f"/ui/tickets/{t['id']}")
    r = admin_client.post(f"/ui/tickets/{t['id']}/messages/{m.id}", data={
        "recipient_email": "c@example.com", "subject": "My careful subject", "body": "My long careful reply",
        "version": stale_version, "csrf_token": csrf})
    assert r.status_code == 409
    assert "My long careful reply" in r.text and "My careful subject" in r.text   # mine, preserved
    assert "Colleague&#39;s body" in r.text or "Colleague's body" in r.text        # theirs, for comparison
    assert "Save my version" in r.text
    assert "Approve &amp; send" not in r.text  # can't approve text you aren't looking at


def test_b11_reapplying_after_conflict_saves_the_agents_version(admin_client, db_session):
    """The re-rendered form carries the CURRENT version, so saving again is a
    deliberate, successful overwrite rather than another conflict."""
    import re
    from tests.conftest import get_csrf_token
    from app import crud
    t, m = _draft_on_ticket(admin_client, db_session)
    stale = m.version
    crud.update_draft(db_session, m.id, "c@example.com", "Theirs", "Theirs", expected_version=m.version)
    csrf = get_csrf_token(admin_client, f"/ui/tickets/{t['id']}")
    mine = {"recipient_email": "c@example.com", "subject": "Mine", "body": "Mine body", "csrf_token": csrf}
    r = admin_client.post(f"/ui/tickets/{t['id']}/messages/{m.id}", data={**mine, "version": stale})
    form_version = re.search(rf'messages/{m.id}" class="message-form">\s*<input[^>]+>\s*<input type="hidden" name="version" value="(\d+)"', r.text).group(1)
    r2 = admin_client.post(f"/ui/tickets/{t['id']}/messages/{m.id}", data={**mine, "version": form_version}, follow_redirects=False)
    assert r2.status_code == 303
    db_session.expire_all()
    assert crud.get_message(db_session, m.id).body == "Mine body"


def test_b11_edit_after_approval_keeps_text_readonly_for_copying(admin_client, db_session):
    from tests.conftest import get_csrf_token
    from app import crud
    t, m = _draft_on_ticket(admin_client, db_session)
    stale = m.version
    crud.approve_message(db_session, m.id, actor_user_id=1, expected_version=m.version)
    csrf = get_csrf_token(admin_client, f"/ui/tickets/{t['id']}")
    r = admin_client.post(f"/ui/tickets/{t['id']}/messages/{m.id}", data={
        "recipient_email": "c@example.com", "subject": "Late edit", "body": "Text I don't want to lose",
        "version": stale, "csrf_token": csrf})
    assert r.status_code == 409
    assert "Text I don&#39;t want to lose" in r.text or "Text I don't want to lose" in r.text
    assert "approved in the meantime" in r.text


def test_b11_invalid_new_draft_keeps_what_was_typed(admin_client):
    from tests.conftest import get_csrf_token
    t = admin_client.post("/tickets", json={"description": "b11 new"}).json()
    csrf = get_csrf_token(admin_client, f"/ui/tickets/{t['id']}")
    r = admin_client.post(f"/ui/tickets/{t['id']}/messages", data={
        "recipient_email": "not-an-email", "subject": "Keep me", "body": "Three paragraphs of work", "csrf_token": csrf})
    assert r.status_code == 422
    assert "Three paragraphs of work" in r.text and 'value="Keep me"' in r.text
