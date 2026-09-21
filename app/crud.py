"""
CRUD (Create/Read/Update) functions.

Keeping these separate from main.py's route handlers is a small but real
architectural choice: it means the core logic isn't tangled up with HTTP
concerns (status codes, request parsing). You could swap FastAPI for
something else later and reuse everything in this file unchanged.
"""
import json
import re
from datetime import datetime, timezone, timedelta
from typing import Optional

from sqlalchemy.orm import Session

from app import models
from app.models import User, Session as SessionModel, Invite, UserRole, Customer, Contact, AuditEvent
from app.rules import compute_priority
from app.security import hash_password, generate_token


def _utcnow() -> datetime:
    """Naive UTC now - datetime.utcnow() is deprecated on newer Python,
    but a naive datetime is what this project's plain DateTime columns
    actually store/return on both SQLite and Postgres (see
    get_active_session's docstring for why naive-vs-aware consistency
    matters here). This is the non-deprecated equivalent."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


class VersionConflict(Exception):
    """
    Raised when a write's expected_version doesn't match the ticket's
    actual current version in the database - i.e. someone else changed
    it since the caller last read it. This is optimistic concurrency
    control: rather than locking the row for the whole edit, we let the
    write proceed only if nothing else won the race first, and surface
    a clear, specific error instead of silently overwriting a concurrent
    change - the exact failure mode the brief calls out ("stale writes
    with useful errors... concurrent ticket edits conflict safely").
    """
    def __init__(self, ticket_id: int, expected_version: int, actual_version: int):
        self.ticket_id = ticket_id
        self.expected_version = expected_version
        self.actual_version = actual_version
        super().__init__(
            f"Ticket {ticket_id}: expected version {expected_version}, "
            f"but it's actually at version {actual_version} - someone else changed it first."
        )


def _record_audit_event(db: Session, actor_user_id: Optional[int], action: str, resource_type: str, resource_id: int, details: Optional[dict] = None) -> None:
    """
    Writes one audit row. `actor_user_id` is nullable: a write made via
    the shared JSON-API key (app/auth.py's require_api_key) has no real
    per-human identity behind it - a shared secret can't prove WHICH
    person acted, so recording a fake actor would be worse than
    recording none. Writes made through the browser UI always have a
    real logged-in user and always pass a real actor_user_id - see
    main.py's UI routes.

    Callers are responsible for committing in the SAME transaction as
    the change this describes - see each write function below, where
    the event add() happens before the single db.commit() that also
    saves the actual change, so the two can never be partially
    committed (one without the other).
    """
    event = AuditEvent(
        actor_user_id=actor_user_id,
        action=action,
        resource_type=resource_type,
        resource_id=resource_id,
        details=json.dumps(details) if details is not None else None,
    )
    db.add(event)


def get_audit_events_for_ticket(db: Session, ticket_id: int) -> list[AuditEvent]:
    return (
        db.query(AuditEvent)
        .filter(AuditEvent.resource_type == "ticket", AuditEvent.resource_id == ticket_id)
        .order_by(AuditEvent.created_at.asc())
        .all()
    )


# --- Tickets ---

def create_ticket(db: Session, description: str, customer_id: Optional[int] = None) -> models.Ticket:
    ticket = models.Ticket(
        description=description,
        priority=compute_priority(description),
        status=models.TicketStatus.open,
        customer_id=customer_id,
    )
    db.add(ticket)
    db.commit()
    db.refresh(ticket)
    return ticket


def get_ticket(db: Session, ticket_id: int) -> Optional[models.Ticket]:
    return db.query(models.Ticket).filter(models.Ticket.id == ticket_id).first()


def list_tickets(
    db: Session,
    status: Optional[str] = None,
    priority: Optional[str] = None,
    q: Optional[str] = None,
    limit: int = 20,
    offset: int = 0,
) -> list[models.Ticket]:
    query = db.query(models.Ticket)
    if status:
        query = query.filter(models.Ticket.status == status)
    if priority:
        query = query.filter(models.Ticket.priority == priority)
    if q:
        query = query.filter(models.Ticket.description.ilike(f"%{q}%"))
    return (
        query.order_by(models.Ticket.created_at.desc())
        .offset(offset)
        .limit(limit)
        .all()
    )


def list_other_tickets(db: Session, exclude_id: int, limit: int = 300) -> list[models.Ticket]:
    """
    All tickets except `exclude_id`, most recent first, capped at
    `limit`. Used by app/related_tickets.py as the candidate pool to
    compare a ticket against. The cap exists because that comparison
    embeds every candidate on the fly (see related_tickets.py) - fine
    at portfolio scale, but a real cap rather than an unbounded query.
    """
    return (
        db.query(models.Ticket)
        .filter(models.Ticket.id != exclude_id)
        .order_by(models.Ticket.created_at.desc())
        .limit(limit)
        .all()
    )


def get_customer_tickets(db: Session, customer_id: int, limit: int = 50) -> list[models.Ticket]:
    """
    A customer's EXACT ticket history - filtered by the real
    `customer_id` foreign key, not similarity. Deliberately separate
    from app/related_tickets.py's semantic matching: the brief is
    explicit that these two must never be conflated, since a
    similar-sounding ticket from a DIFFERENT customer is not that
    customer's history and must never be presented as if it were.
    """
    return (
        db.query(models.Ticket)
        .filter(models.Ticket.customer_id == customer_id)
        .order_by(models.Ticket.created_at.desc())
        .limit(limit)
        .all()
    )


def get_ticket_stats(db: Session) -> dict:
    """
    Counts for the ledger's "at a glance" header. Real, queried numbers -
    not decorative. `high_priority` is specifically open+in_progress
    high-priority tickets (a resolved high-priority ticket isn't
    something that still needs attention).
    """
    total = db.query(models.Ticket).count()
    open_count = db.query(models.Ticket).filter(models.Ticket.status == models.TicketStatus.open).count()
    in_progress = db.query(models.Ticket).filter(models.Ticket.status == models.TicketStatus.in_progress).count()
    resolved = db.query(models.Ticket).filter(models.Ticket.status == models.TicketStatus.resolved).count()
    high_priority_open = (
        db.query(models.Ticket)
        .filter(
            models.Ticket.priority == models.TicketPriority.high,
            models.Ticket.status != models.TicketStatus.resolved,
        )
        .count()
    )
    return {
        "total": total,
        "open": open_count,
        "in_progress": in_progress,
        "resolved": resolved,
        "high_priority_open": high_priority_open,
    }


def update_status(db: Session, ticket_id: int, status: str, expected_version: int, actor_user_id: Optional[int] = None) -> Optional[models.Ticket]:
    """
    Raises VersionConflict if `expected_version` doesn't match the
    ticket's current version (someone else changed it first). Returns
    None if the ticket doesn't exist. Writes an audit event in the same
    transaction as the status change.
    """
    ticket = get_ticket(db, ticket_id)
    if ticket is None:
        return None
    if ticket.version != expected_version:
        raise VersionConflict(ticket_id, expected_version, ticket.version)

    old_status = ticket.status.value if hasattr(ticket.status, "value") else ticket.status
    ticket.status = status
    ticket.version += 1
    _record_audit_event(
        db, actor_user_id, "ticket.status_changed", "ticket", ticket_id,
        details={"from": old_status, "to": status},
    )
    db.commit()
    db.refresh(ticket)
    return ticket


def confirm_category(db: Session, ticket_id: int, category: str, expected_version: int, actor_user_id: Optional[int] = None) -> Optional[models.Ticket]:
    """
    Sets the CONFIRMED category. This is the human-review gate in action:
    nothing else in the system is allowed to set category_confirmed=True
    except this explicit function, called from an explicit user action -
    and now, unlike before, WHO confirmed it and WHEN is recorded too
    (an audit event), not just that it happened. Raises VersionConflict
    on a stale write, same as update_status.
    """
    ticket = get_ticket(db, ticket_id)
    if ticket is None:
        return None
    if ticket.version != expected_version:
        raise VersionConflict(ticket_id, expected_version, ticket.version)

    old_category = ticket.category
    ticket.category = category
    ticket.category_confirmed = 1
    ticket.version += 1
    _record_audit_event(
        db, actor_user_id, "ticket.category_confirmed", "ticket", ticket_id,
        details={"from": old_category, "to": category},
    )
    db.commit()
    db.refresh(ticket)
    return ticket


def assign_ticket(db: Session, ticket_id: int, assignee_user_id: Optional[int], expected_version: int, actor_user_id: Optional[int] = None) -> Optional[models.Ticket]:
    """Assigns (or, with assignee_user_id=None, unassigns) a ticket.
    Same version-conflict and audit-trail pattern as the other writes."""
    ticket = get_ticket(db, ticket_id)
    if ticket is None:
        return None
    if ticket.version != expected_version:
        raise VersionConflict(ticket_id, expected_version, ticket.version)

    old_assignee = ticket.assignee_id
    ticket.assignee_id = assignee_user_id
    ticket.version += 1
    _record_audit_event(
        db, actor_user_id, "ticket.assigned", "ticket", ticket_id,
        details={"from": old_assignee, "to": assignee_user_id},
    )
    db.commit()
    db.refresh(ticket)
    return ticket


def link_ticket_to_customer(db: Session, ticket_id: int, customer_id: Optional[int], expected_version: int, actor_user_id: Optional[int] = None) -> Optional[models.Ticket]:
    """Links (or unlinks) a ticket to a customer record."""
    ticket = get_ticket(db, ticket_id)
    if ticket is None:
        return None
    if ticket.version != expected_version:
        raise VersionConflict(ticket_id, expected_version, ticket.version)

    old_customer = ticket.customer_id
    ticket.customer_id = customer_id
    ticket.version += 1
    _record_audit_event(
        db, actor_user_id, "ticket.customer_linked", "ticket", ticket_id,
        details={"from": old_customer, "to": customer_id},
    )
    db.commit()
    db.refresh(ticket)
    return ticket


# --- Customers & contacts ---

def normalize_phone(raw: str) -> str:
    """
    Strips everything except a leading `+` and digits, then strips a
    US/Canada country code if present, so "+1 (555) 123-4567" and
    "555-123-4567" - both realistic ways the SAME number gets entered -
    normalize to the same value. NOT full E.164 validation or
    international region handling - a real phone integration
    (Milestone 4) would use a proper library (e.g. libphonenumber) for
    that. This is enough to make exact-match lookup work for
    consistently-entered US contact data, and is a deliberately named
    simplification, not a hidden one. (Confirmed via a real test with
    two differently-formatted versions of the same number - without the
    country-code stripping below, they didn't match.)
    """
    if not raw:
        return ""
    digits = re.sub(r"[^\d+]", "", raw)
    if digits.startswith("+1") and len(digits) == 12:
        digits = digits[2:]
    elif digits.startswith("1") and len(digits) == 11:
        digits = digits[1:]
    return digits


def create_customer(db: Session, name: str, notes: Optional[str] = None) -> Customer:
    customer = Customer(name=name, notes=notes)
    db.add(customer)
    db.commit()
    db.refresh(customer)
    return customer


def get_customer(db: Session, customer_id: int) -> Optional[Customer]:
    return db.query(Customer).filter(Customer.id == customer_id).first()


def search_customers(db: Session, q: Optional[str] = None, limit: int = 20) -> list[Customer]:
    query = db.query(Customer)
    if q:
        query = query.filter(Customer.name.ilike(f"%{q}%"))
    return query.order_by(Customer.name.asc()).limit(limit).all()


def create_contact(db: Session, customer_id: int, name: str, email: Optional[str] = None, phone: Optional[str] = None) -> Contact:
    contact = Contact(
        customer_id=customer_id, name=name, email=email, phone=phone,
        normalized_phone=normalize_phone(phone) if phone else None,
    )
    db.add(contact)
    db.commit()
    db.refresh(contact)
    return contact


def get_contact(db: Session, contact_id: int) -> Optional[Contact]:
    return db.query(Contact).filter(Contact.id == contact_id).first()


def list_contacts_for_customer(db: Session, customer_id: int) -> list[Contact]:
    return db.query(Contact).filter(Contact.customer_id == customer_id).order_by(Contact.name.asc()).all()


def find_contacts_by_phone(db: Session, raw_phone: str) -> list[Contact]:
    """
    Caller-ID-style lookup by phone number. Returns a LIST, not a single
    contact - the brief is explicit that a phone number is a lookup
    hint, not identity verification (numbers can be shared, recycled,
    or simply match more than one saved contact), so the caller of this
    function must still have an agent confirm which (if any) result is
    actually the right person before linking or disclosing anything.
    """
    normalized = normalize_phone(raw_phone)
    if not normalized:
        return []
    return db.query(Contact).filter(Contact.normalized_phone == normalized).all()


# --- Agent identity (Milestone 1) ---

def get_user_by_email(db: Session, email: str) -> Optional[User]:
    return db.query(User).filter(User.email == email).first()


def get_user(db: Session, user_id: int) -> Optional[User]:
    return db.query(User).filter(User.id == user_id).first()


def list_agents(db: Session) -> list[User]:
    """Active admin/agent users - for populating an assignment dropdown.
    Reviewers are excluded since they can't be assigned work."""
    return (
        db.query(User)
        .filter(User.is_active == True, User.role.in_([UserRole.admin, UserRole.agent]))  # noqa: E712
        .order_by(User.name.asc())
        .all()
    )


def create_user(db: Session, email: str, name: str, password: str, role: UserRole) -> User:
    user = User(email=email, name=name, password_hash=hash_password(password), role=role)
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def create_session(db: Session, user_id: int, ttl_hours: int) -> SessionModel:
    session = SessionModel(
        id=generate_token(),
        user_id=user_id,
        expires_at=_utcnow() + timedelta(hours=ttl_hours),
    )
    db.add(session)
    db.commit()
    db.refresh(session)
    return session


def get_active_session(db: Session, session_id: str) -> Optional[SessionModel]:
    """A session is active if it exists, isn't revoked, and hasn't expired.
    All three checks happen here so nothing else has to remember to.

    Uses naive UTC (_utcnow(), see above) throughout, matching how this
    project's plain `DateTime` columns actually round-trip on both
    SQLite and Postgres (a `timestamp without time zone` column returns
    a naive datetime either way) - mixing naive and timezone-aware
    datetimes here would raise a TypeError on comparison, which is a
    real, easy-to-hit bug worth avoiding deliberately rather than by luck.
    """
    session = db.query(SessionModel).filter(SessionModel.id == session_id).first()
    if session is None:
        return None
    if session.revoked_at is not None:
        return None
    if session.expires_at < _utcnow():
        return None
    return session


def touch_session(db: Session, session: SessionModel) -> None:
    session.last_used_at = _utcnow()
    db.commit()


def revoke_session(db: Session, session_id: str) -> None:
    session = db.query(SessionModel).filter(SessionModel.id == session_id).first()
    if session is not None:
        session.revoked_at = _utcnow()
        db.commit()


def create_invite(db: Session, email: str, role: UserRole, invited_by_user_id: int, ttl_hours: int) -> Invite:
    invite = Invite(
        token=generate_token(),
        email=email,
        role=role,
        invited_by_user_id=invited_by_user_id,
        expires_at=_utcnow() + timedelta(hours=ttl_hours),
    )
    db.add(invite)
    db.commit()
    db.refresh(invite)
    return invite


def get_valid_invite(db: Session, token: str) -> Optional[Invite]:
    invite = db.query(Invite).filter(Invite.token == token).first()
    if invite is None:
        return None
    if invite.used_at is not None:
        return None
    if invite.expires_at < _utcnow():
        return None
    return invite


def mark_invite_used(db: Session, invite: Invite) -> None:
    invite.used_at = _utcnow()
    db.commit()


# --- Milestone 2: draft/approve/outbox (outbound email) ---

from app.models import OutboundMessage, OutboxJob, MessageStatus, OutboxJobStatus


class MessageNotDraft(Exception):
    """Raised when an edit is attempted on a message that's already
    been approved (or beyond). Editing content/recipient after approval
    is exactly the thing this whole workflow exists to prevent."""
    def __init__(self, message_id: int, status: str):
        self.message_id = message_id
        self.status = status
        super().__init__(f"Message {message_id} is {status}, not a draft - it can no longer be edited.")


def create_draft(db: Session, ticket_id: int, recipient_email: str, subject: str, body: str, created_by_user_id: int) -> OutboundMessage:
    message = OutboundMessage(
        ticket_id=ticket_id, recipient_email=recipient_email, subject=subject, body=body,
        created_by_user_id=created_by_user_id, status=MessageStatus.draft,
    )
    db.add(message)
    db.commit()
    db.refresh(message)
    return message


def get_message(db: Session, message_id: int) -> Optional[OutboundMessage]:
    return db.query(OutboundMessage).filter(OutboundMessage.id == message_id).first()


def list_messages_for_ticket(db: Session, ticket_id: int) -> list[OutboundMessage]:
    return (
        db.query(OutboundMessage)
        .filter(OutboundMessage.ticket_id == ticket_id)
        .order_by(OutboundMessage.created_at.desc())
        .all()
    )


def update_draft(db: Session, message_id: int, recipient_email: str, subject: str, body: str, expected_version: int) -> Optional[OutboundMessage]:
    """Edits a draft's live fields. Raises MessageNotDraft if the
    message has already been approved - this is the enforcement point
    for "editing after approval is not allowed" on the write path (the
    UI also hides the edit form once approved - this is the real
    guarantee, that isn't)."""
    message = get_message(db, message_id)
    if message is None:
        return None
    if message.status != MessageStatus.draft:
        raise MessageNotDraft(message_id, message.status.value)
    if message.version != expected_version:
        raise VersionConflict(message_id, expected_version, message.version)

    message.recipient_email = recipient_email
    message.subject = subject
    message.body = body
    message.version += 1
    db.commit()
    db.refresh(message)
    return message


def approve_message(db: Session, message_id: int, actor_user_id: int, expected_version: int) -> Optional[OutboundMessage]:
    """
    THE core transactional-outbox operation. Takes an immutable
    snapshot of the exact recipient/subject/body being approved, AND
    creates the OutboxJob that will actually send it - in the SAME
    db.commit(), so there is no window where a message is approved but
    never queued (or queued without ever having been approved). The
    OutboxJob's operation_key is derived deterministically from the
    message ID, not freshly generated - so even if this function were
    somehow called twice for the same message (it can't be: the
    `status != draft` check below and the route's own guard both
    prevent it, and the column-level UNIQUE constraint on
    OutboxJob.message_id is a third, structural backstop), only one
    job could ever exist for it.
    """
    message = get_message(db, message_id)
    if message is None:
        return None
    if message.status != MessageStatus.draft:
        raise MessageNotDraft(message_id, message.status.value)
    if message.version != expected_version:
        raise VersionConflict(message_id, expected_version, message.version)

    now = _utcnow()
    message.status = MessageStatus.approved
    message.approved_by_user_id = actor_user_id
    message.approved_at = now
    message.approved_recipient_email = message.recipient_email
    message.approved_subject = message.subject
    message.approved_body = message.body
    message.version += 1

    job = OutboxJob(
        operation_key=f"message-{message_id}",
        message_id=message_id,
        status=OutboxJobStatus.pending,
        next_attempt_at=now,
    )
    db.add(job)

    _record_audit_event(
        db, actor_user_id, "message.approved", "outbound_message", message_id,
        details={"recipient": message.approved_recipient_email},
    )

    db.commit()
    db.refresh(message)
    return message


def get_outbox_job_for_message(db: Session, message_id: int) -> Optional[OutboxJob]:
    return db.query(OutboxJob).filter(OutboxJob.message_id == message_id).first()


# --- Worker-side outbox operations (see worker.py) ---

def claim_next_job(db: Session, worker_id: str, lease_seconds: int) -> Optional[OutboxJob]:
    """
    Atomically claims one job: either genuinely pending-and-due, or
    claimed-by-a-worker-whose-lease-expired (crash recovery - a worker
    that died mid-job leaves it claimed forever otherwise). Uses a
    compare-and-swap UPDATE ... WHERE status=<the status we just read>
    pattern rather than SELECT ... FOR UPDATE SKIP LOCKED, specifically
    so this works unchanged on SQLite (tests, local dev) as well as
    Postgres - the WHERE-matches-old-status UPDATE is atomic at the
    single-row level on both, which is all the correctness this needs.
    Verified directly with two concurrent DB sessions racing for the
    same job - see tests/test_outbox.py.
    """
    from sqlalchemy import or_, and_

    now = _utcnow()
    lease_until = now + timedelta(seconds=lease_seconds)

    candidate = (
        db.query(OutboxJob)
        .filter(
            or_(
                and_(OutboxJob.status == OutboxJobStatus.pending, OutboxJob.next_attempt_at <= now),
                and_(OutboxJob.status == OutboxJobStatus.claimed, OutboxJob.leased_until < now),
            )
        )
        .order_by(OutboxJob.next_attempt_at.asc())
        .first()
    )
    if candidate is None:
        return None

    previous_status = candidate.status
    updated_rows = (
        db.query(OutboxJob)
        .filter(OutboxJob.id == candidate.id, OutboxJob.status == previous_status)
        .update(
            {
                "status": OutboxJobStatus.claimed,
                "leased_by": worker_id,
                "leased_until": lease_until,
                "attempts": OutboxJob.attempts + 1,
                "updated_at": now,
            },
            synchronize_session=False,
        )
    )
    db.commit()
    if updated_rows == 0:
        # Someone else claimed it between our SELECT and our UPDATE -
        # not an error, just a lost race. The caller should try again
        # for a different job.
        return None

    db.refresh(candidate)
    return candidate


def complete_job(db: Session, job_id: int, provider_message_id: Optional[str]) -> None:
    """Marks a job (and its message) as successfully sent. Called by
    the worker after a successful adapter.send()."""
    job = db.query(OutboxJob).filter(OutboxJob.id == job_id).first()
    if job is None:
        return
    job.status = OutboxJobStatus.sent
    job.provider_message_id = provider_message_id
    job.updated_at = _utcnow()

    message = get_message(db, job.message_id)
    if message is not None:
        message.status = MessageStatus.sent

    db.commit()


def fail_job(db: Session, job_id: int, error: str, backoff_seconds: int) -> None:
    """
    Records a failed send attempt. If attempts have reached
    max_attempts, the job becomes terminally `failed` (and the message
    too) - a bounded retry policy, not infinite retries. Otherwise it's
    rescheduled with the given backoff and left `pending` for another
    worker (or the same one, later) to pick up again.
    """
    job = db.query(OutboxJob).filter(OutboxJob.id == job_id).first()
    if job is None:
        return
    job.last_error = error[:2000]
    job.updated_at = _utcnow()

    if job.attempts >= job.max_attempts:
        job.status = OutboxJobStatus.failed
        message = get_message(db, job.message_id)
        if message is not None:
            message.status = MessageStatus.failed
    else:
        job.status = OutboxJobStatus.pending
        job.next_attempt_at = _utcnow() + timedelta(seconds=backoff_seconds)

    db.commit()


# --- Milestone 3: telephony (calls) ---

from app.models import Call, CallDirection, CallStatus


def upsert_call_from_webhook(
    db: Session,
    twilio_call_sid: str,
    direction: str,
    from_number: str,
    to_number: str,
    status: str,
    duration_seconds: Optional[int] = None,
    recording_url: Optional[str] = None,
) -> Call:
    """
    The core idempotency operation for telephony: Twilio sends multiple
    webhooks per call (initial ringing, then status changes, then a
    final callback with duration/recording) - all carrying the SAME
    CallSid. This looks up by that SID first and updates the existing
    row if found, rather than ever creating a second row for a call
    already being tracked. Only sets `started_at`/`ended_at` at the
    natural transition points (first time we see in-progress; first
    time we see a terminal status) rather than overwriting them on
    every callback.
    """
    call = db.query(Call).filter(Call.twilio_call_sid == twilio_call_sid).first()
    terminal_statuses = {"completed", "failed", "busy", "no-answer", "canceled"}

    if call is None:
        call = Call(
            twilio_call_sid=twilio_call_sid,
            direction=direction,
            from_number=from_number,
            to_number=to_number,
            status=status,
        )
        if status == "in-progress":
            call.started_at = _utcnow()
        db.add(call)
    else:
        if call.status.value != "in-progress" and status == "in-progress":
            call.started_at = _utcnow()
        call.status = status

    if status in terminal_statuses and call.ended_at is None:
        call.ended_at = _utcnow()
    if duration_seconds is not None:
        call.duration_seconds = duration_seconds
    if recording_url is not None:
        call.recording_url = recording_url

    # Caller-ID lookup, only on first sight of the call and only when
    # unambiguous - see find_contacts_by_phone's own docstring for why
    # a multi-match or zero-match result leaves this unlinked rather
    # than guessing.
    if call.contact_id is None:
        matches = find_contacts_by_phone(db, from_number if direction == "inbound" else to_number)
        if len(matches) == 1:
            call.contact_id = matches[0].id

    db.commit()
    db.refresh(call)
    return call


def get_call_by_sid(db: Session, twilio_call_sid: str) -> Optional[Call]:
    return db.query(Call).filter(Call.twilio_call_sid == twilio_call_sid).first()


def list_calls_for_contact(db: Session, contact_id: int) -> list[Call]:
    return db.query(Call).filter(Call.contact_id == contact_id).order_by(Call.created_at.desc()).all()


def list_calls_for_customer(db: Session, customer_id: int) -> list[Call]:
    """All calls across every contact belonging to this customer -
    the phone equivalent of get_customer_tickets's exact history."""
    contact_ids = [c.id for c in list_contacts_for_customer(db, customer_id)]
    if not contact_ids:
        return []
    return (
        db.query(Call)
        .filter(Call.contact_id.in_(contact_ids))
        .order_by(Call.created_at.desc())
        .all()
    )


def list_recent_calls(db: Session, limit: int = 50) -> list[Call]:
    """The call log: every call, most recent first, matched or not -
    lets an agent find and manually reconcile an unmatched caller."""
    return db.query(Call).order_by(Call.created_at.desc()).limit(limit).all()


def link_call_to_ticket(db: Session, call_id: int, ticket_id: int) -> Optional[Call]:
    call = db.query(Call).filter(Call.id == call_id).first()
    if call is None:
        return None
    call.ticket_id = ticket_id
    db.commit()
    db.refresh(call)
    return call


# --- Knowledge base management ---

from app.models import KnowledgeArticle


def list_kb_articles(db: Session, q: Optional[str] = None) -> list[KnowledgeArticle]:
    query = db.query(KnowledgeArticle)
    if q:
        query = query.filter(
            (KnowledgeArticle.title.ilike(f"%{q}%")) | (KnowledgeArticle.content.ilike(f"%{q}%"))
        )
    return query.order_by(KnowledgeArticle.category.asc(), KnowledgeArticle.title.asc()).all()


def get_kb_article(db: Session, article_id: int) -> Optional[KnowledgeArticle]:
    return db.query(KnowledgeArticle).filter(KnowledgeArticle.id == article_id).first()


def count_kb_articles(db: Session) -> int:
    return db.query(KnowledgeArticle).count()


def create_kb_article(db: Session, title: str, category: str, content: str) -> KnowledgeArticle:
    """
    Inserts the row with a placeholder embedding - the caller (see
    main.py's /kb routes) is responsible for calling
    supportrag.build_rag_index(db) immediately after, which recomputes
    embeddings for the WHOLE corpus (including this new row) and writes
    the real one back. A single new article can't be embedded in
    isolation - the TF-IDF/SVD space is fit across all articles at
    once, so a standalone embedding here would live in the wrong space.
    """
    article = KnowledgeArticle(title=title, category=category, content=content)
    article.embedding = "[]"
    db.add(article)
    db.commit()
    db.refresh(article)
    return article


def update_kb_article(db: Session, article_id: int, title: str, category: str, content: str) -> Optional[KnowledgeArticle]:
    """Same embedding-recompute caveat as create_kb_article - the
    caller must rebuild the RAGIndex after this returns."""
    article = get_kb_article(db, article_id)
    if article is None:
        return None
    article.title = title
    article.category = category
    article.content = content
    db.commit()
    db.refresh(article)
    return article


def delete_kb_article(db: Session, article_id: int) -> bool:
    """Returns False if the article didn't exist. Callers (see
    main.py) are responsible for enforcing the "at least 2 articles
    must remain" rule BEFORE calling this - see
    supportrag.build_rag_index's own guard for why fewer than 2 breaks
    the embedding space entirely, not just degrades it."""
    article = get_kb_article(db, article_id)
    if article is None:
        return False
    db.delete(article)
    db.commit()
    return True


# --- Reporting / analytics ---

def get_average_resolution_hours(db: Session) -> Optional[float]:
    """
    Average time from a ticket's creation to the FIRST time it became
    resolved, in hours. Computed from the audit trail (an actual
    recorded event), not a "resolved_at" column - this project doesn't
    have one, and the audit trail is already the authoritative record
    of when a status change actually happened. Only counts the first
    resolution per ticket (ordered by event time) so a
    reopened-then-re-resolved ticket doesn't get double-counted or
    measured from its second resolution instead of context that matters
    more (how long the FIRST resolution took).
    """
    import json

    events = (
        db.query(AuditEvent, models.Ticket.created_at.label("ticket_created_at"))
        .join(models.Ticket, AuditEvent.resource_id == models.Ticket.id)
        .filter(AuditEvent.resource_type == "ticket", AuditEvent.action == "ticket.status_changed")
        .order_by(AuditEvent.created_at.asc())
        .all()
    )

    resolution_hours = []
    seen_ticket_ids = set()
    for event, ticket_created_at in events:
        if event.resource_id in seen_ticket_ids:
            continue
        if not event.details:
            continue
        try:
            details = json.loads(event.details)
        except (ValueError, TypeError):
            continue
        if details.get("to") != "resolved":
            continue
        seen_ticket_ids.add(event.resource_id)
        delta_hours = (event.created_at - ticket_created_at).total_seconds() / 3600
        if delta_hours >= 0:  # defensive - a negative delta would mean corrupted data, not a real measurement
            resolution_hours.append(delta_hours)

    if not resolution_hours:
        return None
    return sum(resolution_hours) / len(resolution_hours)


def get_agent_workload(db: Session) -> list[dict]:
    """Active (open + in_progress) ticket count per agent, most-loaded
    first - the "who's overloaded right now" view."""
    agents = list_agents(db)
    result = []
    for agent in agents:
        open_count = db.query(models.Ticket).filter(
            models.Ticket.assignee_id == agent.id, models.Ticket.status == models.TicketStatus.open
        ).count()
        in_progress_count = db.query(models.Ticket).filter(
            models.Ticket.assignee_id == agent.id, models.Ticket.status == models.TicketStatus.in_progress
        ).count()
        result.append({
            "agent": agent, "open": open_count, "in_progress": in_progress_count,
            "active_total": open_count + in_progress_count,
        })
    result.sort(key=lambda r: -r["active_total"])
    return result


def get_reports_data(db: Session) -> dict:
    """
    Everything the /reports page needs, in one call - avoids the page
    re-querying the same ticket table five separate times. Real
    queries over data already being tracked (status, priority,
    category, the audit trail, assignment) - no new tables, no
    estimates.
    """
    from collections import Counter

    tickets = db.query(models.Ticket).all()
    by_status = Counter(t.status.value for t in tickets)
    by_priority = Counter(t.priority.value for t in tickets)
    by_category = Counter((t.category or "Uncategorized") for t in tickets)

    return {
        "total": len(tickets),
        "by_status": dict(by_status),
        "by_priority": dict(by_priority),
        "by_category": dict(sorted(by_category.items(), key=lambda kv: -kv[1])),
        "average_resolution_hours": get_average_resolution_hours(db),
        "agent_workload": get_agent_workload(db),
    }


# --- SLA timers & escalation ---

def compute_sla_deadline(ticket: models.Ticket, sla_hours: dict) -> datetime:
    """The deadline is always ticket.created_at + the hours configured
    for its priority - deliberately simple (not business-hours-aware,
    not pausable while waiting on the customer). `sla_hours` is a plain
    dict ({"high": 4.0, "medium": 24.0, "low": 72.0}), not a Settings
    object - keeps this module decoupled from app.config, matching how
    every other config-dependent crud function (e.g. create_session's
    ttl_hours) takes plain values, not the settings object itself."""
    hours = sla_hours[ticket.priority.value]
    return ticket.created_at + timedelta(hours=hours)


def is_ticket_breached(ticket: models.Ticket, sla_hours: dict, now: Optional[datetime] = None) -> bool:
    """A resolved ticket is never "breached" in the active sense, no
    matter how late it was resolved - this is about what still needs
    attention right now, not a historical compliance record (which
    would be a different, not-yet-built report)."""
    if ticket.status.value == "resolved":
        return False
    now = now or _utcnow()
    return now > compute_sla_deadline(ticket, sla_hours)


def list_breached_tickets(db: Session, sla_hours: dict) -> list[models.Ticket]:
    """Every currently-overdue, still-open ticket, most overdue first -
    the "needs attention" view."""
    candidates = db.query(models.Ticket).filter(models.Ticket.status != models.TicketStatus.resolved).all()
    now = _utcnow()
    breached = [(t, compute_sla_deadline(t, sla_hours)) for t in candidates]
    breached = [(t, deadline) for t, deadline in breached if now > deadline]
    breached.sort(key=lambda pair: pair[1])
    return [t for t, _ in breached]


def record_new_sla_breaches(db: Session, sla_hours: dict) -> int:
    """
    Finds tickets that are breached AND don't already have a
    `ticket.sla_breached` audit event, and logs one for each - this is
    what sla_check.py calls on every poll cycle. Idempotent by design:
    checking for an existing event before logging a new one is what
    makes it safe to call this every minute forever without spamming
    the audit trail with the same breach over and over. Returns how
    many NEW breaches were recorded this call (0 most of the time - a
    ticket usually breaches once, gets noticed, and gets worked, not
    re-breaches on every poll).
    """
    breached_tickets = list_breached_tickets(db, sla_hours)
    newly_recorded = 0
    for ticket in breached_tickets:
        already_logged = (
            db.query(AuditEvent)
            .filter(
                AuditEvent.resource_type == "ticket",
                AuditEvent.resource_id == ticket.id,
                AuditEvent.action == "ticket.sla_breached",
            )
            .first()
        )
        if already_logged is not None:
            continue
        _record_audit_event(
            db, actor_user_id=None, action="ticket.sla_breached", resource_type="ticket",
            resource_id=ticket.id, details={"priority": ticket.priority.value},
        )
        newly_recorded += 1
    if newly_recorded:
        db.commit()
    return newly_recorded
