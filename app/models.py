"""
The Ticket data model.

Deliberately minimal for Week 1: no AI fields yet (those come in Week 4+
when we add category suggestion). Get this right first.
"""
import enum
import json
from datetime import datetime, timezone
from sqlalchemy import Column, Integer, String, DateTime, Enum as SAEnum, Text
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


class KnowledgeArticle(Base):
    """
    A knowledge-base entry used by SupportRAG (Project 2) to suggest a
    category and draft a response for a new ticket, by retrieving the
    most similar articles and citing them.

    `embedding` is stored as a JSON-encoded list of floats in a Text
    column, not a native vector column. This is a deliberate portability
    choice: it works unchanged on plain SQLite (this project's default)
    with no extra setup. The production upgrade path - already scaffolded
    in docker-compose.yml - is to switch this column to pgvector's
    `Vector` type on Postgres, which lets similarity search run as an
    indexed `ORDER BY embedding <=> query_embedding` in the database
    instead of the current cosine_similarity() loop in embeddings.py.
    That swap only touches this model and the retrieval query in
    supportrag.py - nothing else in the app depends on the storage format.
    """

    __tablename__ = "knowledge_articles"

    id = Column(Integer, primary_key=True, index=True)
    title = Column(String, nullable=False)
    category = Column(String, nullable=False)
    content = Column(Text, nullable=False)
    embedding = Column(Text, nullable=False)  # JSON-encoded list[float]

    def get_embedding(self) -> list[float]:
        return json.loads(self.embedding)

    def set_embedding(self, vector: list[float]) -> None:
        self.embedding = json.dumps(vector)
