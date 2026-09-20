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
