"""
The Ticket data model.

Deliberately minimal for Week 1: no AI fields yet (those come in Week 4+
when we add category suggestion). Get this right first.
"""
import enum
import json
from datetime import datetime, timezone


def _utcnow() -> datetime:
    """Naive UTC now - datetime.utcnow() is deprecated on newer Python;
    this is the non-deprecated equivalent, kept naive to match how this
    project's plain DateTime columns round-trip on SQLite and Postgres."""
    return datetime.now(timezone.utc).replace(tzinfo=None)
from sqlalchemy import Column, Integer, String, DateTime, Enum as SAEnum, Text, Boolean, ForeignKey
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

    # Nullable by design: existing tickets from before the customer model
    # existed have no customer to point at, and the brief is explicit
    # that migrating old data shouldn't mean deleting or faking it.
    customer_id = Column(Integer, ForeignKey("customers.id"), nullable=True, index=True)
    assignee_id = Column(Integer, ForeignKey("users.id"), nullable=True, index=True)

    # Optimistic concurrency: every update must include the version it
    # was read at (see crud.py's update functions). A stale write - one
    # based on an older version than what's actually in the database -
    # is rejected with a clear conflict rather than silently overwriting
    # someone else's concurrent change. Starts at 1, not 0, so "the
    # version I read" is never confused with "no version provided".
    version = Column(Integer, nullable=False, default=1)

    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))


class UserRole(str, enum.Enum):
    """
    Three roles, matching what a small support team actually needs:
    admin (manages agents, full access), agent (normal ticket work),
    reviewer (read-only - can see everything, change nothing). This is
    deliberately NOT a generic permissions system - a small, fixed role
    set is easier to reason about and to defend in an interview than a
    flexible-but-unverified permissions framework would be.
    """
    admin = "admin"
    agent = "agent"
    reviewer = "reviewer"


class User(Base):
    """
    A real agent account. Created only via the invite flow (see Invite
    below) - there is no open self-registration route, matching the
    brief's "invite-only account creation" requirement.
    """
    __tablename__ = "users"

    id = Column(Integer, primary_key=True, index=True)
    email = Column(String, unique=True, nullable=False, index=True)
    name = Column(String, nullable=False)
    password_hash = Column(String, nullable=False)  # Argon2id, never plaintext - see app/security.py
    role = Column(SAEnum(UserRole), nullable=False, default=UserRole.agent)
    is_active = Column(Boolean, nullable=False, default=True)
    created_at = Column(DateTime, default=_utcnow)


class Session(Base):
    """
    An opaque server-side session - NOT a JWT. The token itself carries
    no information; it's just a lookup key. This is deliberate: a
    server-side session can be revoked instantly (logout, admin
    deactivation, suspected compromise) by deleting/marking this one
    row, which a self-contained signed token cannot do without an
    additional revocation-list mechanism anyway. Matches the brief's
    explicit "expiring opaque server-side sessions... rotation/
    revocation" requirement, and OWASP session-management guidance.
    """
    __tablename__ = "sessions"

    id = Column(String, primary_key=True)  # random opaque token, see security.generate_token()
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    created_at = Column(DateTime, default=_utcnow)
    expires_at = Column(DateTime, nullable=False)
    revoked_at = Column(DateTime, nullable=True)  # set on logout or explicit revocation
    last_used_at = Column(DateTime, default=_utcnow)


class Invite(Base):
    """
    A one-use, expiring invite token - this is the ONLY way a new User
    row gets created. An admin creates one (specifying the invitee's
    email and role); the invitee visits a link containing `token` and
    sets their own password, which is the only point their real
    password ever exists outside their own head.

    No email-sending system exists yet (that's Milestone 2's job) - for
    now, creating an invite returns the link directly to the admin, who
    is expected to deliver it out-of-band. This is a real, documented
    simplification, not a security gap: the token itself is a random,
    unguessable, expiring, one-use secret regardless of how it's
    delivered to the invitee.
    """
    __tablename__ = "invites"

    id = Column(Integer, primary_key=True, index=True)
    token = Column(String, unique=True, nullable=False, index=True)
    email = Column(String, nullable=False)
    role = Column(SAEnum(UserRole), nullable=False, default=UserRole.agent)
    invited_by_user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    created_at = Column(DateTime, default=_utcnow)
    expires_at = Column(DateTime, nullable=False)
    used_at = Column(DateTime, nullable=True)


class Customer(Base):
    """
    A customer COMPANY - a record, not a tenant. Per the brief's product
    decision: this is one support organization serving many customer
    companies, not multi-tenant SaaS. Contacts (people) belong to a
    customer; tickets link to a customer, optionally, through a contact.
    """
    __tablename__ = "customers"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String, nullable=False)
    notes = Column(Text, nullable=True)
    created_at = Column(DateTime, default=_utcnow)


class Contact(Base):
    """
    A person at a customer company. `normalized_phone` strips everything
    except leading `+` and digits (see crud.normalize_phone) so a
    caller-ID lookup can match "+1 (555) 123-4567" against a contact
    saved as "555-123-4567" - a real, if intentionally simple,
    normalization; a production phone integration (Milestone 4) would
    use a proper library (e.g. Google's libphonenumber) for full E.164
    validation and region handling, which this deliberately doesn't do.
    """
    __tablename__ = "contacts"

    id = Column(Integer, primary_key=True, index=True)
    customer_id = Column(Integer, ForeignKey("customers.id"), nullable=False, index=True)
    name = Column(String, nullable=False)
    email = Column(String, nullable=True)
    phone = Column(String, nullable=True)
    normalized_phone = Column(String, nullable=True, index=True)
    created_at = Column(DateTime, default=_utcnow)


class AuditEvent(Base):
    """
    An append-only record of who did what, to what, when. This exists
    specifically to close a gap the production brief called out
    correctly: `Ticket.category_confirmed` being a boolean is not proof
    a human acted - there was no record of WHICH agent, or when. Every
    write this project treats as a real decision (confirming a category,
    changing status, assigning a ticket) should also write one of these,
    in the same transaction as the change itself.

    Deliberately generic (resource_type/resource_id, not a foreign key
    per resource type) so one audit table covers tickets now and other
    resource types later without a schema change each time - a small,
    real tradeoff: it costs a join-by-convention instead of a real FK,
    in exchange for not needing a new audit table per resource type.
    """
    __tablename__ = "audit_events"

    id = Column(Integer, primary_key=True, index=True)
    actor_user_id = Column(Integer, ForeignKey("users.id"), nullable=True, index=True)
    action = Column(String, nullable=False)  # e.g. "ticket.category_confirmed"
    resource_type = Column(String, nullable=False)  # e.g. "ticket"
    resource_id = Column(Integer, nullable=False, index=True)
    details = Column(Text, nullable=True)  # JSON-encoded extra context, e.g. {"category": "hardware"}
    created_at = Column(DateTime, default=_utcnow)


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
