"""
Genuine concurrent-session race tests for the ticket/message
compare-and-swap fix in app/crud.py.

An external production-readiness review found a real bug here: every
ticket-write function used to read a row, check `version` in PYTHON,
then write - a TOCTOU race where two concurrent requests could both
pass the check before either committed. The project's own EARLIER
sequential stale-write tests (call once, then again with the old
version) never caught this, because they only prove the check fires on
a SECOND call - they say nothing about two calls racing at the same
instant. These tests use a real threading.Barrier to force that
instant, on two genuinely separate DB sessions - not the same session
called twice, which would prove nothing about concurrent access.

Also verified once, separately, against real Postgres with two truly
separate connections (SQLite's StaticPool shares one connection, so
even with a barrier the database operations still serialize there -
the barrier's value on SQLite is exercising genuine interleaving at
the point the calls are issued, not truly concurrent I/O) - see
docs/current-state.md for that verification.
"""
import threading

import pytest

from app.security import hash_password
from tests.conftest import engine as _test_engine

# These tests genuinely race two OS threads against the SAME database
# connection when running on SQLite (tests/conftest.py's StaticPool
# shares one physical connection across "separate" sessions, and
# SQLite's check_same_thread=False override - needed elsewhere for
# TestClient's own threading - does not make concurrent access from
# two real OS threads well-defined at the C library level). This
# produces genuine, confirmed flakiness (run repeatedly by hand: same
# code, different wrong outcomes including "both threads lost the
# race" - not a timing coincidence, a real property of racing two
# threads on one shared connection). That flakiness is about the TEST
# INFRASTRUCTURE's database choice, not about crud.py's correctness -
# the exact same tests pass reliably, every time, against real
# Postgres with genuinely separate connections (confirmed by running
# them repeatedly there too). Skipped on SQLite rather than left
# flaky - a test that fails unpredictably for reasons unrelated to the
# code under test erodes trust in the whole suite. Run with
# TEST_DATABASE_URL=postgresql://... to exercise these for real - see
# docs/current-state.md for CI's own SQLite+Postgres split, which
# already runs the full suite against Postgres for exactly this class
# of test.
pytestmark = pytest.mark.skipif(
    _test_engine.dialect.name != "postgresql",
    reason="Genuine concurrent-thread races need real separate DB connections - "
           "SQLite's shared StaticPool connection makes this flaky for reasons "
           "unrelated to the code under test. Run with TEST_DATABASE_URL=postgresql://... instead.",
)


def _make_user_and_ticket(db_session, email="race@example.com"):
    from app import crud
    from app.models import User, UserRole
    user = User(email=email, name="Race Test", password_hash=hash_password("x"), role=UserRole.admin)
    db_session.add(user)
    db_session.commit()
    ticket = crud.create_ticket(db_session, "race test ticket")
    return user, ticket


def test_two_sessions_racing_on_update_status_only_one_wins(db_session):
    """The exact race the review demonstrated by hand: two sessions
    both read version=1, both race to write - exactly one must
    succeed, the other must get VersionConflict, and the ticket must
    end at version=2 (one increment), never 3 (double-applied) and
    never left at 1 (lost update)."""
    from app import crud
    from tests.conftest import TestSessionLocal

    user, ticket = _make_user_and_ticket(db_session)
    ticket_id, starting_version = ticket.id, ticket.version

    barrier = threading.Barrier(2)
    results = []
    results_lock = threading.Lock()

    def race(worker_label, new_status):
        session = TestSessionLocal()
        try:
            barrier.wait()
            try:
                updated = crud.update_status(session, ticket_id, new_status, expected_version=starting_version, actor_user_id=user.id)
                with results_lock:
                    results.append((worker_label, "won", updated.version))
            except crud.VersionConflict:
                with results_lock:
                    results.append((worker_label, "conflict", None))
        finally:
            session.close()

    t1 = threading.Thread(target=race, args=("A", "in_progress"))
    t2 = threading.Thread(target=race, args=("B", "resolved"))
    t1.start()
    t2.start()
    t1.join()
    t2.join()

    winners = [r for r in results if r[1] == "won"]
    conflicts = [r for r in results if r[1] == "conflict"]
    assert len(winners) == 1, f"expected exactly one winner, got {results}"
    assert len(conflicts) == 1, f"expected exactly one conflict, got {results}"

    db_session.expire_all()  # db_session's own identity map can hold a stale pre-race copy - force a genuine re-read, not a cached one
    final = crud.get_ticket(db_session, ticket_id)
    assert final.version == starting_version + 1, "version must increment exactly once - not lost, not double-applied"
    assert final.status.value in ("in_progress", "resolved")  # whichever genuinely won


def test_two_sessions_racing_on_assign_ticket_only_one_wins(db_session):
    from app import crud
    from app.models import User, UserRole
    from tests.conftest import TestSessionLocal

    user, ticket = _make_user_and_ticket(db_session, email="race2@example.com")
    agent1 = User(email="agent-a@example.com", name="Agent A", password_hash=hash_password("x"), role=UserRole.agent)
    agent2 = User(email="agent-b@example.com", name="Agent B", password_hash=hash_password("x"), role=UserRole.agent)
    db_session.add_all([agent1, agent2])
    db_session.commit()
    ticket_id, starting_version = ticket.id, ticket.version
    agent1_id, agent2_id = agent1.id, agent2.id

    barrier = threading.Barrier(2)
    results = []
    results_lock = threading.Lock()

    def race(assignee_id):
        session = TestSessionLocal()
        try:
            barrier.wait()
            try:
                updated = crud.assign_ticket(session, ticket_id, assignee_id, expected_version=starting_version, actor_user_id=user.id)
                with results_lock:
                    results.append(("won", updated.assignee_id))
            except crud.VersionConflict:
                with results_lock:
                    results.append(("conflict", None))
        finally:
            session.close()

    t1 = threading.Thread(target=race, args=(agent1_id,))
    t2 = threading.Thread(target=race, args=(agent2_id,))
    t1.start()
    t2.start()
    t1.join()
    t2.join()

    winners = [r for r in results if r[0] == "won"]
    assert len(winners) == 1, f"expected exactly one winner, got {results}"

    db_session.expire_all()
    final = crud.get_ticket(db_session, ticket_id)
    assert final.version == starting_version + 1
    assert final.assignee_id in (agent1_id, agent2_id)


def test_draft_edit_racing_approval_named_specifically_by_the_review(db_session):
    """
    The exact scenario the external review called out by name: "a draft
    edit racing approval can undermine the relationship between the
    displayed draft and its approved snapshot." One session tries to
    edit the draft's content; another tries to approve it at the same
    instant. Exactly one must win. If approval wins, the edit must be
    rejected (not silently lost) - and critically, the approved
    snapshot must NOT contain content from a rejected concurrent edit
    that never actually landed.
    """
    from app import crud
    from app.models import OutboundMessage, MessageStatus
    from tests.conftest import TestSessionLocal

    user, ticket = _make_user_and_ticket(db_session, email="race3@example.com")
    message = OutboundMessage(
        ticket_id=ticket.id, recipient_email="customer@example.com", subject="Original subject",
        body="Original body", created_by_user_id=user.id, status=MessageStatus.draft,
    )
    db_session.add(message)
    db_session.commit()
    message_id, starting_version, user_id = message.id, message.version, user.id

    barrier = threading.Barrier(2)
    results = []
    results_lock = threading.Lock()

    def try_edit():
        session = TestSessionLocal()
        try:
            barrier.wait()
            try:
                crud.update_draft(session, message_id, "customer@example.com", "EDITED subject", "EDITED body", expected_version=starting_version)
                with results_lock:
                    results.append(("edit", "won"))
            except (crud.VersionConflict, crud.MessageNotDraft):
                with results_lock:
                    results.append(("edit", "lost"))
        finally:
            session.close()

    def try_approve():
        session = TestSessionLocal()
        try:
            barrier.wait()
            try:
                crud.approve_message(session, message_id, actor_user_id=user_id, expected_version=starting_version)
                with results_lock:
                    results.append(("approve", "won"))
            except (crud.VersionConflict, crud.MessageNotDraft):
                with results_lock:
                    results.append(("approve", "lost"))
        finally:
            session.close()

    t1 = threading.Thread(target=try_edit)
    t2 = threading.Thread(target=try_approve)
    t1.start()
    t2.start()
    t1.join()
    t2.join()

    winners = [r for r in results if r[1] == "won"]
    assert len(winners) == 1, f"expected exactly one winner, got {results}"

    db_session.expire_all()
    final = crud.get_message(db_session, message_id)
    if winners[0][0] == "approve":
        # Approval won: the snapshot must reflect the ORIGINAL content,
        # never a partially-applied or rejected concurrent edit.
        assert final.status == MessageStatus.approved
        assert final.approved_subject == "Original subject"
        assert final.approved_body == "Original body"
    else:
        # Edit won: the message must still be an editable draft with
        # the new content, not silently promoted to approved.
        assert final.status == MessageStatus.draft
        assert final.subject == "EDITED subject"


def test_two_sessions_racing_to_approve_the_same_message_only_one_job_created(db_session):
    """The double-click scenario, verified as a genuine race (not the
    sequential version already covered in test_milestone1_features.py):
    two concurrent approval attempts on the same draft must produce
    exactly one OutboxJob, never two, never zero."""
    from app import crud
    from app.models import OutboundMessage, MessageStatus, OutboxJob
    from tests.conftest import TestSessionLocal

    user, ticket = _make_user_and_ticket(db_session, email="race4@example.com")
    message = OutboundMessage(
        ticket_id=ticket.id, recipient_email="customer@example.com", subject="Subject",
        body="Body", created_by_user_id=user.id, status=MessageStatus.draft,
    )
    db_session.add(message)
    db_session.commit()
    message_id, starting_version, user_id = message.id, message.version, user.id

    barrier = threading.Barrier(2)
    results = []
    results_lock = threading.Lock()

    def race():
        session = TestSessionLocal()
        try:
            barrier.wait()
            try:
                crud.approve_message(session, message_id, actor_user_id=user_id, expected_version=starting_version)
                with results_lock:
                    results.append("won")
            except (crud.VersionConflict, crud.MessageNotDraft):
                with results_lock:
                    results.append("lost")
        finally:
            session.close()

    t1 = threading.Thread(target=race)
    t2 = threading.Thread(target=race)
    t1.start()
    t2.start()
    t1.join()
    t2.join()

    assert results.count("won") == 1, f"expected exactly one winner, got {results}"
    db_session.expire_all()
    jobs = db_session.query(OutboxJob).filter(OutboxJob.message_id == message_id).all()
    assert len(jobs) == 1, f"expected exactly one outbox job, got {len(jobs)}"
