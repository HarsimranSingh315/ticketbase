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
from sqlalchemy import update as sa_update

from app import models
from app.models import User, Session as SessionModel, Invite, UserRole, Customer, Contact, AuditEvent
from app.rules import compute_priority
from app.security import hash_password, generate_token, hash_token


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


def _atomic_ticket_update(
    db: Session, ticket_id: int, expected_version: int, changes: dict,
    actor_user_id: Optional[int], action: str, audit_details: dict,
) -> Optional[models.Ticket]:
    """
    The real compare-and-swap fix for a genuine, externally-confirmed
    bug: every ticket-write function below used to read a row, check
    `version` in PYTHON, then write - a TOCTOU race. Two concurrent
    requests could both read the same version, both pass the Python
    check before either committed, and the second would silently
    overwrite the first with no conflict ever raised. Verified directly
    against two genuinely concurrent Postgres sessions racing on the
    same ticket, which is what actually exposed it (sequential
    stale-write tests - call once, then again with the old version -
    only prove the check fires on a SECOND call; they don't touch this
    race at all). See tests/test_concurrency_races.py.

    This does what claim_next_job (below) already did correctly for
    outbox jobs: a single real `UPDATE ... WHERE id=:id AND
    version=:expected` statement. The database itself is what enforces
    "only if nothing changed since you read it" - not a Python `if`
    that runs milliseconds before a separate write. `changes` must
    include a fresh `version` value (typically `models.Ticket.version
    + 1`, a SQL-side expression, not a Python-side increment - the
    increment has to happen in the SAME atomic statement as the
    conflict check, or it reintroduces exactly the race being fixed).

    Returns None if the ticket doesn't exist at all. Raises
    VersionConflict if it exists but wasn't at expected_version -
    these are deliberately distinguished (404 vs 409), which needs one
    extra read, but only on the (uncommon) failure path.
    """
    result = db.execute(
        sa_update(models.Ticket)
        .where(models.Ticket.id == ticket_id, models.Ticket.version == expected_version)
        .values(**changes)
    )
    if result.rowcount == 0:
        # commit() here, not rollback() - matching claim_next_job's proven-safe
        # pattern exactly. An UPDATE matching zero rows makes no changes, so
        # committing it is always safe - and found, by actually running a genuine
        # concurrent-session test, to matter beyond tidiness: on SQLite's shared
        # StaticPool connection, one thread's rollback() can collide with another
        # thread's already-completed commit() on the SAME physical connection,
        # raising "cannot commit - no transaction is active". commit() doesn't
        # have that failure mode. Also expires the identity map, same as any commit.
        db.commit()
        ticket = get_ticket(db, ticket_id)
        if ticket is None:
            return None
        raise VersionConflict(ticket_id, expected_version, ticket.version)

    _record_audit_event(db, actor_user_id, action, "ticket", ticket_id, details=audit_details)
    db.commit()  # expire_on_commit=True (the default, unchanged in app/database.py) means the read below is genuinely fresh, not the identity map's stale copy
    return get_ticket(db, ticket_id)


def update_status(db: Session, ticket_id: int, status: str, expected_version: int, actor_user_id: Optional[int] = None) -> Optional[models.Ticket]:
    """
    Raises VersionConflict if `expected_version` doesn't match the
    ticket's current version (someone else changed it first). Returns
    None if the ticket doesn't exist. Writes an audit event in the same
    transaction as the status change. See _atomic_ticket_update's
    docstring for why this is a real atomic UPDATE, not a Python-side
    check.
    """
    current = get_ticket(db, ticket_id)
    if current is None:
        return None
    old_status = current.status.value if hasattr(current.status, "value") else current.status
    return _atomic_ticket_update(
        db, ticket_id, expected_version,
        changes={"status": status, "version": models.Ticket.version + 1},
        actor_user_id=actor_user_id, action="ticket.status_changed",
        audit_details={"from": old_status, "to": status},
    )


def confirm_category(db: Session, ticket_id: int, category: str, expected_version: int, actor_user_id: Optional[int] = None) -> Optional[models.Ticket]:
    """
    Sets the CONFIRMED category. This is the human-review gate in action:
    nothing else in the system is allowed to set category_confirmed=True
    except this explicit function, called from an explicit user action -
    and now, unlike before, WHO confirmed it and WHEN is recorded too
    (an audit event), not just that it happened. Raises VersionConflict
    on a stale write, same as update_status.
    """
    current = get_ticket(db, ticket_id)
    if current is None:
        return None
    old_category = current.category
    return _atomic_ticket_update(
        db, ticket_id, expected_version,
        changes={"category": category, "category_confirmed": 1, "version": models.Ticket.version + 1},
        actor_user_id=actor_user_id, action="ticket.category_confirmed",
        audit_details={"from": old_category, "to": category},
    )


def assign_ticket(db: Session, ticket_id: int, assignee_user_id: Optional[int], expected_version: int, actor_user_id: Optional[int] = None) -> Optional[models.Ticket]:
    """Assigns (or, with assignee_user_id=None, unassigns) a ticket.
    Same version-conflict and audit-trail pattern as the other writes."""
    current = get_ticket(db, ticket_id)
    if current is None:
        return None
    old_assignee = current.assignee_id
    return _atomic_ticket_update(
        db, ticket_id, expected_version,
        changes={"assignee_id": assignee_user_id, "version": models.Ticket.version + 1},
        actor_user_id=actor_user_id, action="ticket.assigned",
        audit_details={"from": old_assignee, "to": assignee_user_id},
    )


def link_ticket_to_customer(db: Session, ticket_id: int, customer_id: Optional[int], expected_version: int, actor_user_id: Optional[int] = None) -> Optional[models.Ticket]:
    """Links (or unlinks) a ticket to a customer record."""
    current = get_ticket(db, ticket_id)
    if current is None:
        return None
    old_customer = current.customer_id
    return _atomic_ticket_update(
        db, ticket_id, expected_version,
        changes={"customer_id": customer_id, "version": models.Ticket.version + 1},
        actor_user_id=actor_user_id, action="ticket.customer_linked",
        audit_details={"from": old_customer, "to": customer_id},
    )


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


def create_session(db: Session, user_id: int, ttl_hours: int) -> tuple[str, SessionModel]:
    """
    Returns (raw_token, session) - the raw token is what goes in the
    cookie (see main.py's login/accept_invite routes); only its hash
    is ever written to the database. See hash_token's docstring for
    why storing the raw value was a real gap, not a style preference.
    """
    raw_token = generate_token()
    session = SessionModel(
        id=hash_token(raw_token),
        user_id=user_id,
        expires_at=_utcnow() + timedelta(hours=ttl_hours),
    )
    db.add(session)
    db.commit()
    db.refresh(session)
    return raw_token, session


def get_active_session(db: Session, session_id: str) -> Optional[SessionModel]:
    """A session is active if it exists, isn't revoked, and hasn't expired.
    All three checks happen here so nothing else has to remember to.

    `session_id` is the RAW value read from the cookie - hashed here
    before the lookup, since only the hash is ever stored (see
    hash_token's docstring).

    Uses naive UTC (_utcnow(), see above) throughout, matching how this
    project's plain `DateTime` columns actually round-trip on both
    SQLite and Postgres (a `timestamp without time zone` column returns
    a naive datetime either way) - mixing naive and timezone-aware
    datetimes here would raise a TypeError on comparison, which is a
    real, easy-to-hit bug worth avoiding deliberately rather than by luck.
    """
    session = db.query(SessionModel).filter(SessionModel.id == hash_token(session_id)).first()
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
    """`session_id` is the RAW cookie value - hashed before lookup,
    same reason as get_active_session."""
    session = db.query(SessionModel).filter(SessionModel.id == hash_token(session_id)).first()
    if session is not None:
        session.revoked_at = _utcnow()
        db.commit()


def create_invite(db: Session, email: str, role: UserRole, invited_by_user_id: int, ttl_hours: int) -> tuple[str, Invite]:
    """Returns (raw_token, invite) - the raw token goes in the invite
    LINK shown to the admin; only its hash is stored, same reasoning as
    create_session."""
    raw_token = generate_token()
    invite = Invite(
        token=hash_token(raw_token),
        email=email,
        role=role,
        invited_by_user_id=invited_by_user_id,
        expires_at=_utcnow() + timedelta(hours=ttl_hours),
    )
    db.add(invite)
    db.commit()
    db.refresh(invite)
    return raw_token, invite


def get_valid_invite(db: Session, token: str) -> Optional[Invite]:
    """`token` is the RAW value from the invite link's query string -
    hashed before lookup, same reasoning as get_active_session."""
    invite = db.query(Invite).filter(Invite.token == hash_token(token)).first()
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


def _atomic_message_update(db: Session, message_id: int, expected_version: int, changes: dict) -> Optional[OutboundMessage]:
    """
    Same compare-and-swap fix as _atomic_ticket_update, for messages -
    also checks status='draft' in the SAME atomic WHERE clause, not as
    a separate Python check. This closes the exact race the external
    review named by pointing at this function specifically: "a draft
    edit racing approval can undermine the relationship between the
    displayed draft and its approved snapshot" - a concurrent approval
    changes both version AND status, and the old Python-side check
    only ever compared version, not status, so a well-timed edit could
    still land after an approval's read but before its write.
    """
    result = db.execute(
        sa_update(OutboundMessage)
        .where(
            OutboundMessage.id == message_id,
            OutboundMessage.version == expected_version,
            OutboundMessage.status == MessageStatus.draft,
        )
        .values(**changes)
    )
    if result.rowcount == 0:
        db.commit()  # not rollback() - see _atomic_ticket_update's comment for why that matters, not just style
        message = get_message(db, message_id)
        if message is None:
            return None
        if message.status != MessageStatus.draft:
            raise MessageNotDraft(message_id, message.status.value)
        raise VersionConflict(message_id, expected_version, message.version)

    db.commit()
    return get_message(db, message_id)


def update_draft(db: Session, message_id: int, recipient_email: str, subject: str, body: str, expected_version: int) -> Optional[OutboundMessage]:
    """Edits a draft's live fields. Raises MessageNotDraft if the
    message has already been approved - this is the enforcement point
    for "editing after approval is not allowed" on the write path (the
    UI also hides the edit form once approved - this is the real
    guarantee, that isn't). See _atomic_message_update's docstring for
    why the draft-status check is now inside the same atomic UPDATE as
    the version check, not a separate Python comparison."""
    return _atomic_message_update(
        db, message_id, expected_version,
        changes={
            "recipient_email": recipient_email, "subject": subject, "body": body,
            "version": OutboundMessage.version + 1,
        },
    )


def approve_message(db: Session, message_id: int, actor_user_id: int, expected_version: int, max_attempts: Optional[int] = None) -> Optional[OutboundMessage]:
    """
    THE core transactional-outbox operation. Takes an immutable
    snapshot of the exact recipient/subject/body being approved, AND
    creates the OutboxJob that will actually send it - in the SAME
    db.commit(), so there is no window where a message is approved but
    never queued (or queued without ever having been approved). The
    OutboxJob's operation_key is derived deterministically from the
    message ID, not freshly generated - so even if this function were
    somehow called twice for the same message, only one job could ever
    exist for it (the column-level UNIQUE constraint on
    OutboxJob.message_id is the structural backstop).

    Now a real atomic `UPDATE ... WHERE id=:id AND version=:expected
    AND status='draft'`, not a read-then-write - see
    _atomic_message_update's docstring for the race this closes. The
    approved_* snapshot columns are set via `SET approved_subject =
    subject` (a same-row column reference evaluated BY THE DATABASE,
    not a value read moments earlier by Python) - so the snapshot is
    guaranteed to reflect exactly what the row held at the instant this
    UPDATE's WHERE clause matched, which is the actual point of an
    "immutable approved snapshot" in the first place.

    `max_attempts`: pass `settings.outbox_max_attempts` from the caller
    - left optional (falls back to the model's own default of 5) only
    so existing direct callers (tests) don't break, not because
    skipping it is fine for a real deployment. An earlier version
    always used the model default regardless of what was actually
    configured - a real, if quiet, gap: a deployment that set
    OUTBOX_MAX_ATTEMPTS to something other than 5 had that setting
    silently ignored for every job.
    """
    now = _utcnow()
    result = db.execute(
        sa_update(OutboundMessage)
        .where(
            OutboundMessage.id == message_id,
            OutboundMessage.version == expected_version,
            OutboundMessage.status == MessageStatus.draft,
        )
        .values(
            status=MessageStatus.approved,
            approved_by_user_id=actor_user_id,
            approved_at=now,
            approved_recipient_email=OutboundMessage.recipient_email,
            approved_subject=OutboundMessage.subject,
            approved_body=OutboundMessage.body,
            version=OutboundMessage.version + 1,
        )
    )
    if result.rowcount == 0:
        db.commit()  # not rollback() - see _atomic_ticket_update's comment for why that matters, not just style
        message = get_message(db, message_id)
        if message is None:
            return None
        if message.status != MessageStatus.draft:
            raise MessageNotDraft(message_id, message.status.value)
        raise VersionConflict(message_id, expected_version, message.version)

    job = OutboxJob(
        operation_key=f"message-{message_id}",
        message_id=message_id,
        status=OutboxJobStatus.pending,
        next_attempt_at=now,
        **({"max_attempts": max_attempts} if max_attempts is not None else {}),
    )
    db.add(job)

    # Reads the just-written (still-uncommitted, same-transaction)
    # approved_recipient_email for the audit record - a fresh read, not
    # a stale identity-mapped object, since this is the first read of
    # this row in this function.
    updated_message = get_message(db, message_id)
    _record_audit_event(
        db, actor_user_id, "message.approved", "outbound_message", message_id,
        details={"recipient": updated_message.approved_recipient_email},
    )

    db.commit()
    return get_message(db, message_id)


def get_outbox_job_for_message(db: Session, message_id: int) -> Optional[OutboxJob]:
    return db.query(OutboxJob).filter(OutboxJob.message_id == message_id).first()


# --- Worker-side outbox operations (see worker.py) ---

def claim_next_job(db: Session, worker_id: str, lease_seconds: int) -> Optional[OutboxJob]:
    """
    Atomically claims one job: either genuinely pending-and-due, or
    claimed-by-a-worker-whose-lease-expired (crash recovery - a worker
    that died mid-job leaves it claimed forever otherwise).

    Two real bugs here, found by an external review and independently
    confirmed by tracing the code, fixed together:

    1. The compare-and-swap for a FRESH claim (`WHERE status='pending'`)
       was already correct - a fresh claim genuinely changes status
       from 'pending' to 'claimed'. But the RECLAIM case set status to
       the SAME value it already had ('claimed' -> 'claimed'), so a
       WHERE clause checking only status couldn't tell "this specific
       expired lease" from "a lease someone else already reclaimed a
       moment ago" - two concurrent reclaimers could both match. Fixed
       by also requiring `lease_token` to match the exact value read
       for THIS lease generation (see OutboxJob.lease_token's
       docstring) - a fresh, unique token is assigned on every
       successful claim, so this genuinely pins one generation.
    2. Nothing enforced the attempt ceiling AT CLAIM TIME - only
       fail_job did, which a crashed worker never reaches. A job that
       kept crashing its worker could be reclaimed forever, each
       reclaim incrementing attempts with no upper bound ever actually
       enforced. Fixed with an upfront pass that terminally fails any
       expired-claimed job already at its limit (the crash meant
       nothing else would ever notice), and by excluding
       already-at-limit jobs from being reclaimed as work in the first
       place.
    """
    from sqlalchemy import or_, and_

    now = _utcnow()
    lease_until = now + timedelta(seconds=lease_seconds)

    # Pass 1: terminally fail expired-claimed jobs already at their
    # attempt ceiling - a real atomic UPDATE, not a read-then-write, so
    # two workers doing this pass at the same moment can't double-apply
    # it (harmless if they did - both would set the same terminal
    # state). Must ALSO fail the associated message, in the SAME
    # transaction - caught by testing this specific path directly: an
    # earlier version updated only the OutboxJob row, leaving the
    # OutboundMessage stuck at `approved` forever even though its send
    # had genuinely, terminally failed.
    stuck_job_rows = (
        db.query(OutboxJob.id, OutboxJob.message_id)
        .filter(
            OutboxJob.status == OutboxJobStatus.claimed,
            OutboxJob.leased_until < now,
            OutboxJob.attempts >= OutboxJob.max_attempts,
        )
        .all()
    )
    if stuck_job_rows:
        stuck_job_ids = [row.id for row in stuck_job_rows]
        stuck_message_ids = [row.message_id for row in stuck_job_rows]
        db.execute(
            sa_update(OutboxJob)
            .where(OutboxJob.id.in_(stuck_job_ids))
            .values(
                status=OutboxJobStatus.failed,
                last_error="Worker crashed repeatedly and exceeded max_attempts on lease expiry (never reached a normal failed-send path).",
                updated_at=now,
            )
        )
        db.execute(
            sa_update(OutboundMessage)
            .where(OutboundMessage.id.in_(stuck_message_ids))
            .values(status=MessageStatus.failed)
        )
        db.commit()

    candidate = (
        db.query(OutboxJob)
        .filter(
            or_(
                and_(OutboxJob.status == OutboxJobStatus.pending, OutboxJob.next_attempt_at <= now),
                and_(
                    OutboxJob.status == OutboxJobStatus.claimed,
                    OutboxJob.leased_until < now,
                    OutboxJob.attempts < OutboxJob.max_attempts,
                ),
            )
        )
        .order_by(OutboxJob.next_attempt_at.asc())
        .first()
    )
    if candidate is None:
        return None

    new_token = generate_token()
    if candidate.status == OutboxJobStatus.pending:
        where_clause = (OutboxJob.id == candidate.id, OutboxJob.status == OutboxJobStatus.pending)
    else:
        # Reclaim: pin the EXACT lease generation we just read, via its
        # own current lease_token - status alone can't distinguish this
        # from a fresher reclaim (see this function's docstring).
        # SQLAlchemy compiles `== None` to `IS NULL` automatically, so
        # this is correct even for a job claimed before this column
        # existed (lease_token still NULL from an old row).
        where_clause = (
            OutboxJob.id == candidate.id,
            OutboxJob.status == OutboxJobStatus.claimed,
            OutboxJob.lease_token == candidate.lease_token,
        )

    updated_rows = (
        db.query(OutboxJob)
        .filter(*where_clause)
        .update(
            {
                "status": OutboxJobStatus.claimed,
                "leased_by": worker_id,
                "leased_until": lease_until,
                "lease_token": new_token,
                "attempts": OutboxJob.attempts + 1,
                "updated_at": now,
            },
            synchronize_session=False,
        )
    )
    db.commit()
    if updated_rows == 0:
        # Someone else claimed (or reclaimed) it between our SELECT and
        # our UPDATE - not an error, just a lost race. The caller
        # should try again for a different job.
        return None

    db.refresh(candidate)
    return candidate


def complete_job(db: Session, job_id: int, lease_token: str, provider_message_id: Optional[str]) -> None:
    """
    Marks a job (and its message) as successfully sent. Called by the
    worker after a successful adapter.send(). Requires the caller's
    remembered `lease_token` to still match - if it doesn't (the lease
    expired and was reclaimed by another worker since this one started
    its send attempt), this is a STALE completion from a worker that's
    no longer the active owner, and is safely ignored rather than
    allowed to overwrite whatever the current, active worker is doing
    (or has already done). Found and fixed after an external review
    named this exact scenario: "stale workers can overwrite results."
    """
    result = db.execute(
        sa_update(OutboxJob)
        .where(OutboxJob.id == job_id, OutboxJob.lease_token == lease_token, OutboxJob.status == OutboxJobStatus.claimed)
        .values(status=OutboxJobStatus.sent, provider_message_id=provider_message_id, updated_at=_utcnow())
    )
    if result.rowcount == 0:
        db.commit()  # commit, not rollback, even on this no-op path - see claim_next_job's reasoning; matters, not just style
        return

    job = db.query(OutboxJob).filter(OutboxJob.id == job_id).first()
    message = get_message(db, job.message_id)
    if message is not None:
        message.status = MessageStatus.sent
    db.commit()


def fail_job(db: Session, job_id: int, lease_token: str, error: str, backoff_seconds: int) -> None:
    """
    Records a failed send attempt. If attempts have reached
    max_attempts, the job becomes terminally `failed` (and the message
    too) - a bounded retry policy, not infinite retries. Otherwise it's
    rescheduled with the given backoff and left `pending` for another
    worker (or the same one, later) to pick up again.

    Same stale-completion protection as complete_job: requires the
    caller's `lease_token` to still be the active one, or the failure
    report is safely ignored rather than allowed to revert a job
    another worker has since moved on from (possibly already sent).
    """
    job = (
        db.query(OutboxJob)
        .filter(OutboxJob.id == job_id, OutboxJob.lease_token == lease_token, OutboxJob.status == OutboxJobStatus.claimed)
        .first()
    )
    if job is None:
        db.commit()
        return

    if job.attempts >= job.max_attempts:
        new_status = OutboxJobStatus.failed
        new_next_attempt_at = job.next_attempt_at
    else:
        new_status = OutboxJobStatus.pending
        new_next_attempt_at = _utcnow() + timedelta(seconds=backoff_seconds)

    result = db.execute(
        sa_update(OutboxJob)
        .where(OutboxJob.id == job_id, OutboxJob.lease_token == lease_token, OutboxJob.status == OutboxJobStatus.claimed)
        .values(status=new_status, last_error=error[:2000], next_attempt_at=new_next_attempt_at, updated_at=_utcnow())
    )
    if result.rowcount == 0:
        db.commit()
        return

    if new_status == OutboxJobStatus.failed:
        message = get_message(db, job.message_id)
        if message is not None:
            message.status = MessageStatus.failed
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


def list_calls_for_ticket(db: Session, ticket_id: int) -> list[Call]:
    """Calls explicitly linked to this ticket - an outbound call placed
    from the ticket page (see ui_place_outbound_call) sets this at
    creation time. An inbound call is only linked here if an agent
    connects it manually (crud.link_call_to_ticket) - caller-ID lookup
    alone links a call to a CONTACT, not a specific ticket, since one
    contact can have many tickets and a call isn't inherently about any
    particular one of them."""
    return db.query(Call).filter(Call.ticket_id == ticket_id).order_by(Call.created_at.desc()).all()


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


def create_outbound_call(
    db: Session, call_sid: str, to_number: str, from_number: str,
    contact_id: Optional[int] = None, ticket_id: Optional[int] = None,
) -> Call:
    """Records a call the app itself placed (as opposed to
    upsert_call_from_webhook, which records calls Twilio tells us
    about). Starts at `queued` - the real status arrives via the
    normal /webhooks/twilio/status callback as the call progresses,
    same as any other call once it exists."""
    call = Call(
        twilio_call_sid=call_sid, direction=CallDirection.outbound,
        from_number=from_number, to_number=to_number, status=CallStatus.queued,
        contact_id=contact_id, ticket_id=ticket_id,
    )
    db.add(call)
    db.commit()
    db.refresh(call)
    return call
