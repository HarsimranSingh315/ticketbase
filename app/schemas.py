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
    """What a client sends to change a ticket's status. `version` is the
    ticket's version as the client last read it - required so a stale
    write (based on outdated data) is rejected as a clear 409 conflict
    instead of silently overwriting a concurrent change. See
    crud.VersionConflict."""
    status: TicketStatus
    version: int


class TicketCategoryConfirm(BaseModel):
    """What a client sends to confirm (or override) a ticket's category.
    See TicketStatusUpdate's docstring for why `version` is required."""
    category: str = Field(..., min_length=1, max_length=100)
    version: int


class TicketOut(BaseModel):
    """What the API returns for a single ticket."""
    model_config = ConfigDict(from_attributes=True)

    id: int
    description: str
    category: Optional[str] = None
    category_confirmed: bool = False
    status: TicketStatus
    priority: TicketPriority
    customer_id: Optional[int] = None
    assignee_id: Optional[int] = None
    version: int
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


class RelatedTicketOut(BaseModel):
    """One ticket found similar to another (see app/related_tickets.py)."""
    id: int
    description: str
    status: str
    similarity: float


class CustomerOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    name: str
    notes: Optional[str] = None
    created_at: datetime


class ContactOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    customer_id: int
    name: str
    email: Optional[str] = None
    phone: Optional[str] = None


class AuditEventOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    actor_user_id: Optional[int] = None
    action: str
    details: Optional[str] = None
    created_at: datetime
