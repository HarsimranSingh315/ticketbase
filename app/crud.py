"""
CRUD (Create/Read/Update) functions.

Keeping these separate from main.py's route handlers is a small but real
architectural choice: it means the core logic isn't tangled up with HTTP
concerns (status codes, request parsing). You could swap FastAPI for
something else later and reuse everything in this file unchanged.
"""
from typing import Optional
from sqlalchemy.orm import Session
from app import models
from app.rules import compute_priority


def create_ticket(db: Session, description: str) -> models.Ticket:
    ticket = models.Ticket(
        description=description,
        priority=compute_priority(description),
        status=models.TicketStatus.open,
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


def update_status(db: Session, ticket_id: int, status: str) -> Optional[models.Ticket]:
    ticket = get_ticket(db, ticket_id)
    if ticket is None:
        return None
    ticket.status = status
    db.commit()
    db.refresh(ticket)
    return ticket


def confirm_category(db: Session, ticket_id: int, category: str) -> Optional[models.Ticket]:
    """
    Sets the CONFIRMED category. This is the human-review gate in action:
    nothing else in the system is allowed to set category_confirmed=True
    except this explicit function, called from an explicit user action.
    """
    ticket = get_ticket(db, ticket_id)
    if ticket is None:
        return None
    ticket.category = category
    ticket.category_confirmed = 1
    db.commit()
    db.refresh(ticket)
    return ticket


# --- Agent identity (Milestone 1) ---

from datetime import datetime, timezone, timedelta


def _utcnow() -> datetime:
    """Naive UTC now - _utcnow() is deprecated on newer Python,
    but a naive datetime is what this project's plain DateTime columns
    actually store/return on both SQLite and Postgres (see
    get_active_session's docstring for why naive-vs-aware consistency
    matters here). This is the non-deprecated equivalent."""
    return datetime.now(timezone.utc).replace(tzinfo=None)
from app.models import User, Session as SessionModel, Invite, UserRole
from app.security import hash_password, generate_token


def get_user_by_email(db: Session, email: str) -> Optional[User]:
    return db.query(User).filter(User.email == email).first()


def get_user(db: Session, user_id: int) -> Optional[User]:
    return db.query(User).filter(User.id == user_id).first()


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
