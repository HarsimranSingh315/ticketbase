"""
Pydantic schemas - these validate what comes IN to the API and shape what
goes OUT. Keeping them separate from the SQLAlchemy models (models.py) is
deliberate: the database shape and the API's public contract are allowed
to differ, and often should.
"""
from datetime import datetime
from typing import Optional
from pydantic import BaseModel, Field, ConfigDict, field_validator
from app.models import TicketStatus, TicketPriority


class TicketCreate(BaseModel):
    """What a client sends to create a ticket."""
    description: str = Field(..., min_length=1, max_length=2000)

    @field_validator("description")
    @classmethod
    def description_must_not_be_only_whitespace(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("Description must not be empty or only whitespace.")
        return value


class TicketStatusUpdate(BaseModel):
    """What a client sends to change a ticket's status."""
    status: TicketStatus


class TicketCategoryConfirm(BaseModel):
    """What a client sends to confirm (or override) a ticket's category."""
    category: str = Field(..., min_length=1, max_length=100)


class TicketOut(BaseModel):
    """What the API returns for a single ticket."""
    model_config = ConfigDict(from_attributes=True)

    id: int
    description: str
    category: Optional[str] = None
    category_confirmed: bool = False
    status: TicketStatus
    priority: TicketPriority
    created_at: datetime


class SuggestionSource(BaseModel):
    """One knowledge-base article a suggestion was based on."""
    article_id: int
    title: str
    category: str
    similarity: float


class SuggestionOut(BaseModel):
    """
    What SupportRAG returns for a ticket. Deliberately never includes
    any field that could be mistaken for an already-applied category -
    the caller must still call PATCH /tickets/{id}/category to apply it.
    """
    abstained: bool
    category: Optional[str] = None
    confidence: float
    sources: list[SuggestionSource] = []
    draft_response: str
    draft_source: str = "template"
