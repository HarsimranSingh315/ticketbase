"""
The Ticket data model.

Deliberately minimal for Week 1: no AI fields yet (those come in Week 4+
when we add category suggestion). Get this right first.
"""
import enum
from datetime import datetime, timezone
from sqlalchemy import Column, Integer, String, DateTime, Enum as SAEnum
from app.database import Base


class TicketStatus(str, enum.Enum):
    open = "open"
    in_progress = "in_progress"
    resolved = "resolved"


class TicketPriority(str, enum.Enum):
    low = "low"
    medium = "medium"
    high = "high"


class Ticket(Base):
    __tablename__ = "tickets"

    id = Column(Integer, primary_key=True, index=True)
    description = Column(String, nullable=False)

    # category is nullable and unconfirmed until a human confirms it.
    # This field structure is what makes the "AI suggests, human confirms"
    # rule enforceable later - there's nowhere for an AI to silently apply
    # a category without this confirmation step existing in the data model.
    category = Column(String, nullable=True)
    category_confirmed = Column(Integer, default=0)  # 0 = false, 1 = true (portable across SQLite/Postgres)

    status = Column(SAEnum(TicketStatus), default=TicketStatus.open, nullable=False)
    priority = Column(SAEnum(TicketPriority), default=TicketPriority.medium, nullable=False)

    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
