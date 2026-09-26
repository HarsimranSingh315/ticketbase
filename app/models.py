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



class MessageStatus(str, enum.Enum):
    """
    draft -> approved -> sent (happy path). failed is terminal after
    bounded retries exhaust; bounced is terminal on a provider bounce
    callback (Milestone 2 next-step - webhook handling isn't built
    yet, see README). abstained/rejected states don't exist here on
    purpose - a draft an agent doesn't like is just edited or deleted,
    not "rejected" as a workflow state.
    """
    draft = "draft"
    approved = "approved"
    sent = "sent"
    failed = "failed"
    bounced = "bounced"


class OutboundMessage(Base):
    """
    A drafted (and, once approved, immutably snapshotted) outbound
    email tied to a ticket. The core rule this table enforces: editing
    content or recipient after approval is not allowed - approval binds
    the EXACT recipient/subject/body, timestamp, and approver. If an
    agent wants to change something after approving, that's a NEW
    draft, not an edit to the approved one. `approved_*` columns are a
    snapshot taken at approval time, kept separate from the live
    editable fields, so there's no ambiguity about what was approved
    even if a future code path somehow touched the live fields (defense
    in depth - the routes in main.py already block editing an approved
    message, this is the data-layer backstop for that rule).
    """
    __tablename__ = "outbound_messages"

    id = Column(Integer, primary_key=True, index=True)
    ticket_id = Column(Integer, ForeignKey("tickets.id"), nullable=False, index=True)

    recipient_email = Column(String, nullable=False)
    subject = Column(String, nullable=False)
    body = Column(Text, nullable=False)

    status = Column(SAEnum(MessageStatus), nullable=False, default=MessageStatus.draft)

    created_by_user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    created_at = Column(DateTime, default=_utcnow)

    approved_by_user_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    approved_at = Column(DateTime, nullable=True)
    approved_recipient_email = Column(String, nullable=True)
    approved_subject = Column(String, nullable=True)
    approved_body = Column(Text, nullable=True)

    # Optimistic concurrency, same pattern as Ticket.version.
    version = Column(Integer, nullable=False, default=1)


class OutboxJobStatus(str, enum.Enum):
    pending = "pending"
    claimed = "claimed"
    sent = "sent"
    failed = "failed"       # terminal - bounded retries exhausted
    ambiguous = "ambiguous"  # terminal-ish - needs human reconciliation (see worker.py)


class OutboxJob(Base):
    """
    The transactional outbox: approving a message and creating its
    OutboxJob happen in the SAME db.commit() (see crud.approve_message),
    so there's no window where a message is "approved" but never
    queued, or queued twice. `operation_key` is a stable idempotency
    key derived from the message ID (not a fresh UUID per attempt) -
    re-approving (blocked by the API anyway) or a worker retry can
    never produce two jobs for the same message, enforced by the
    column's own uniqueness, not just application logic remembering to
    check.

    Claiming (`leased_by`/`leased_until`) uses a compare-and-swap
    UPDATE ... WHERE status=<expected> pattern (see
    crud.claim_next_job) rather than SELECT ... FOR UPDATE SKIP LOCKED,
    specifically so the exact same code works on both SQLite (tests,
    local dev) and Postgres (production) - verified with a real
    concurrent-claim test using two separate DB sessions.
    """
    __tablename__ = "outbox_jobs"

    id = Column(Integer, primary_key=True, index=True)
    operation_key = Column(String, unique=True, nullable=False, index=True)
    message_id = Column(Integer, ForeignKey("outbound_messages.id"), nullable=False, unique=True)

    status = Column(SAEnum(OutboxJobStatus), nullable=False, default=OutboxJobStatus.pending)
    attempts = Column(Integer, nullable=False, default=0)
    max_attempts = Column(Integer, nullable=False, default=5)

    leased_by = Column(String, nullable=True)
    leased_until = Column(DateTime, nullable=True)
    # A fresh random token assigned on every successful claim (first
    # claim AND every reclaim of an expired lease) - NOT the same value
    # as `status`, which is exactly the problem this column fixes. A
    # reclaim's compare-and-swap has to distinguish "this specific
    # lease generation" from "a job that happens to currently read as
    # claimed" - `status` alone can't do that, because reclaiming an
    # expired lease sets status to the SAME value it already had
    # ('claimed' -> 'claimed'), so two concurrent reclaimers could both
    # match a WHERE clause that only checks status. lease_token changes
    # on every claim, so it uniquely pins one lease generation - see
    # crud.claim_next_job. complete_job/fail_job require the caller's
    # remembered token to still match this column before changing
    # anything, so a stale worker (its lease already expired and
    # reclaimed by someone else) can never overwrite a newer worker's
    # result - found and fixed after an external review named this
    # exact class of bug.
    lease_token = Column(String, nullable=True)
    next_attempt_at = Column(DateTime, nullable=False, default=_utcnow)

    provider_message_id = Column(String, nullable=True)
    last_error = Column(Text, nullable=True)

    created_at = Column(DateTime, default=_utcnow)
    updated_at = Column(DateTime, default=_utcnow)


class LocalSinkEmail(Base):
    """
    Where "sent" mail actually lands when no real provider is
    configured (the default - see Settings.resend_api_key). This is
    what makes the local sink genuinely useful for review/demo rather
    than a no-op: every email the app would have sent is here,
    inspectable, with the exact idempotency key that was used to send
    it. `idempotency_key` is UNIQUE - this is what makes the local sink
    adapter itself idempotent (see app/mail.py): a second send attempt
    with the same key is detected and treated as already-sent rather
    than creating a duplicate row.
    """
    __tablename__ = "local_sink_emails"

    id = Column(Integer, primary_key=True, index=True)
    idempotency_key = Column(String, unique=True, nullable=False, index=True)
    recipient_email = Column(String, nullable=False)
    subject = Column(String, nullable=False)
    body = Column(Text, nullable=False)
    sent_at = Column(DateTime, default=_utcnow)


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


class CallDirection(str, enum.Enum):
    inbound = "inbound"
    outbound = "outbound"


class CallStatus(str, enum.Enum):
    """Mirrors Twilio's own CallStatus values (https://www.twilio.com/docs/voice/api/call-resource) -
    deliberately using Twilio's own vocabulary rather than inventing a
    parallel one, so a status callback's CallStatus param maps directly
    onto this enum with no translation layer to keep in sync.

    `queued` was added specifically for outbound calling (this project
    initially only received inbound calls, which start at `ringing` -
    an outbound call PLACED via Twilio's REST API starts `queued`
    before Twilio even attempts to ring it). Confirmed this value was
    genuinely missing by testing directly against real Postgres before
    adding it: the native enum type rejected the raw string outright
    (`invalid input value for enum callstatus: "queued"`) - SQLite, by
    contrast, silently accepted it with no enforcement at all, which is
    exactly the kind of divergence this project tests both databases to
    catch, not just assumes about."""
    queued = "queued"
    ringing = "ringing"
    in_progress = "in-progress"
    completed = "completed"
    failed = "failed"
    busy = "busy"
    no_answer = "no-answer"
    canceled = "canceled"


class Call(Base):
    """
    A phone call, inbound or outbound, tracked via Twilio's webhooks.
    `twilio_call_sid` is Twilio's own unique ID for the call and is the
    natural idempotency key here - Twilio retries webhooks and sends
    multiple status callbacks per call (ringing, then in-progress, then
    completed), and every one of them refers to the same CallSid. This
    column being UNIQUE is what lets crud.upsert_call_from_webhook treat
    "have we seen this CallSid before" as a single indexed lookup rather
    than something the application has to get right through logic alone.

    `contact_id` is populated by caller-ID lookup (see
    crud.find_contacts_by_phone) ONLY when the calling number matches
    exactly one contact - same principle as that function's own
    docstring: a phone number is a lookup hint, not identity
    verification, so an ambiguous or absent match leaves this NULL for
    an agent to resolve rather than guessing.
    """
    __tablename__ = "calls"

    id = Column(Integer, primary_key=True, index=True)
    twilio_call_sid = Column(String, unique=True, nullable=False, index=True)
    direction = Column(SAEnum(CallDirection), nullable=False)
    from_number = Column(String, nullable=False)
    to_number = Column(String, nullable=False)
    status = Column(SAEnum(CallStatus), nullable=False, default=CallStatus.ringing)

    contact_id = Column(Integer, ForeignKey("contacts.id"), nullable=True, index=True)
    ticket_id = Column(Integer, ForeignKey("tickets.id"), nullable=True, index=True)

    duration_seconds = Column(Integer, nullable=True)
    recording_url = Column(String, nullable=True)

    started_at = Column(DateTime, nullable=True)
    ended_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=_utcnow)


class KnowledgeBaseState(Base):
    """
    Single-row table holding the knowledge base's revision number.

    B3 (external review, reproduced with two index objects): each web
    process caches its own retrieval index, and a KB edit only rebuilt
    the index in the process that handled it - other processes kept
    serving deleted or outdated articles indefinitely. Every KB mutation
    now increments `revision` in the same transaction; each process
    compares it to the revision its cached index was built from and
    rebuilds when they differ.
    """
    __tablename__ = "kb_state"

    id = Column(Integer, primary_key=True)
    revision = Column(Integer, nullable=False, default=0)


class TicketNote(Base):
    """
    An INTERNAL note on a ticket: agent-to-agent context, never shown to or
    sent to a customer.

    The public/internal boundary is structural, not a flag: notes live in
    their own table, and nothing on the customer-facing path (OutboundMessage,
    OutboxJob, app/mail.py, worker.py) or the external-AI path (app/llm.py)
    reads from it. There is no `is_public` column to set wrongly. Tests in
    tests/test_notes.py assert this boundary directly, including a static
    check that those modules never reference this model.
    """
    __tablename__ = "ticket_notes"

    id = Column(Integer, primary_key=True, index=True)
    ticket_id = Column(Integer, ForeignKey("tickets.id"), nullable=False, index=True)
    author_user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    body = Column(Text, nullable=False)
    created_at = Column(DateTime, default=_utcnow, nullable=False)
