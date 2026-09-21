"""
TicketBase API + web UI.

Run it with:
    uvicorn app.main:app --reload

Then visit http://127.0.0.1:8000/docs for FastAPI's auto-generated,
interactive API documentation.
Visit http://127.0.0.1:8000/ for the web UI (requires agent login - see
Milestone 1 / docs/current-state.md).

Two separate auth mechanisms, for two separate audiences (see app/auth.py
for the full reasoning): a shared API key for machine clients (JSON API,
CLI), and real per-agent sessions (login, roles, CSRF) for the browser
UI. Production-readiness features from earlier milestones (config,
cached SupportRAG index, rate limiting, structured logging, consistent
error responses, pagination) are unchanged - see README for the full list.
"""
import logging
import time
from contextlib import asynccontextmanager
from typing import Optional

from fastapi import FastAPI, Depends, HTTPException, Request, Form, Query
from fastapi.responses import RedirectResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session
from pydantic import ValidationError
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.util import get_remote_address
from slowapi.errors import RateLimitExceeded

from app.config import get_settings, Settings
from app.database import engine, Base, get_db, SessionLocal
from app import crud, schemas
from app.auth import (
    require_api_key, require_agent, require_role, require_csrf,
    get_current_user, csrf_token_for_template, AuthRedirect, SESSION_COOKIE_NAME,
)
from app.models import User, UserRole
from app.related_tickets import find_related_tickets
from app.supportrag import SupportRAGService, RAGIndex, build_rag_index
from app.telephony import validate_twilio_signature

settings = get_settings()

logging.basicConfig(
    level=settings.log_level,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger("ticketbase")


def _bootstrap_admin_if_needed(db: Session) -> None:
    """See Settings.bootstrap_admin_email's docstring - only acts when
    the users table is completely empty, so this can't be used to
    inject a second admin later."""
    if db.query(User).first() is not None:
        return
    if not settings.bootstrap_admin_email or not settings.bootstrap_admin_password:
        logger.warning(
            "No users exist and no BOOTSTRAP_ADMIN_EMAIL/PASSWORD set - "
            "no one can log in. Set those in .env, restart once, then "
            "you can unset them."
        )
        return
    crud.create_user(
        db, email=settings.bootstrap_admin_email, name=settings.bootstrap_admin_name,
        password=settings.bootstrap_admin_password, role=UserRole.admin,
    )
    logger.info("Bootstrap admin account created: %s", settings.bootstrap_admin_email)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Dev-friendly auto-create. Production deployments should instead
    # run `alembic upgrade head` before starting the app - this
    # create_all is a no-op against a DB that's already migrated.
    Base.metadata.create_all(bind=engine)

    db = SessionLocal()
    try:
        app.state.rag_index = build_rag_index(db)
        _bootstrap_admin_if_needed(db)
    finally:
        db.close()

    logger.info("TicketBase startup complete (db=%s)", settings.database_url.split("://")[0])
    yield
    logger.info("TicketBase shutting down")


limiter = Limiter(key_func=get_remote_address)

app = FastAPI(title="TicketBase", version="0.3.0", lifespan=lifespan)
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)
app.mount("/static", StaticFiles(directory="app/static"), name="static")
templates = Jinja2Templates(directory="app/templates")


@app.exception_handler(AuthRedirect)
async def auth_redirect_handler(request: Request, exc: AuthRedirect):
    return RedirectResponse(url=f"/login?next={exc.next_path}", status_code=303)


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception):
    """
    Catch-all so an unexpected error returns a clean JSON 500 instead of
    leaking a raw traceback to the client. The real traceback still goes
    to the server log, where it belongs.
    """
    logger.exception("Unhandled exception on %s %s", request.method, request.url.path)
    return JSONResponse(status_code=500, content={"detail": "Internal server error"})


@app.middleware("http")
async def log_requests(request: Request, call_next):
    start = time.monotonic()
    response = await call_next(request)
    duration_ms = (time.monotonic() - start) * 1000
    logger.info(
        "%s %s -> %d (%.1fms)",
        request.method, request.url.path, response.status_code, duration_ms,
    )
    return response


def get_rag_index(request: Request) -> RAGIndex:
    return request.app.state.rag_index


# --- JSON API (machine clients: CLI, scripts, integrations) ---
# Protected by the shared API key (app/auth.py's require_api_key),
# off by default for local dev/tests.

@app.post("/tickets", response_model=schemas.TicketOut, status_code=201, dependencies=[Depends(require_api_key)])
def create_ticket(payload: schemas.TicketCreate, db: Session = Depends(get_db)):
    ticket = crud.create_ticket(db, payload.description)
    return ticket


@app.get("/tickets", response_model=list[schemas.TicketOut])
def list_tickets(
    status: Optional[str] = None,
    priority: Optional[str] = None,
    q: Optional[str] = None,
    limit: int = Query(default=settings.default_page_size, ge=1, le=settings.max_page_size),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
):
    return crud.list_tickets(db, status=status, priority=priority, q=q, limit=limit, offset=offset)


@app.get("/tickets/{ticket_id}", response_model=schemas.TicketOut)
def get_ticket(ticket_id: int, db: Session = Depends(get_db)):
    ticket = crud.get_ticket(db, ticket_id)
    if ticket is None:
        raise HTTPException(status_code=404, detail=f"Ticket {ticket_id} not found")
    return ticket


@app.patch("/tickets/{ticket_id}/status", response_model=schemas.TicketOut, dependencies=[Depends(require_api_key)])
def update_ticket_status(ticket_id: int, payload: schemas.TicketStatusUpdate, db: Session = Depends(get_db)):
    try:
        ticket = crud.update_status(db, ticket_id, payload.status, expected_version=payload.version)
    except crud.VersionConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    if ticket is None:
        raise HTTPException(status_code=404, detail=f"Ticket {ticket_id} not found")
    return ticket


@app.patch("/tickets/{ticket_id}/category", response_model=schemas.TicketOut, dependencies=[Depends(require_api_key)])
def confirm_ticket_category(ticket_id: int, payload: schemas.TicketCategoryConfirm, db: Session = Depends(get_db)):
    """
    Confirms a ticket's category. A human types this in directly, or an
    AI-suggested category (from /suggest) pre-fills it - but this exact
    same explicit confirmation call is still required either way. The AI
    never gets a shortcut around this endpoint. `payload.version` must
    match the ticket's current version (optimistic concurrency - see
    crud.VersionConflict) - a stale write is rejected with 409, not
    silently applied over a change someone else already made.
    """
    try:
        ticket = crud.confirm_category(db, ticket_id, payload.category, expected_version=payload.version)
    except crud.VersionConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    if ticket is None:
        raise HTTPException(status_code=404, detail=f"Ticket {ticket_id} not found")
    return ticket


@app.post("/tickets/{ticket_id}/suggest", response_model=schemas.SuggestionOut)
@limiter.limit(settings.suggest_rate_limit)
def suggest_ticket_category(
    request: Request, ticket_id: int,
    db: Session = Depends(get_db),
    rag_index: RAGIndex = Depends(get_rag_index),
    rag_settings: Settings = Depends(get_settings),
):
    """
    SupportRAG (Project 2): returns a suggested category + drafted
    response, with cited sources and a confidence score. Read-only -
    never writes to the ticket. Rate-limited since it's the most
    compute-heavy route in the app.
    """
    ticket = crud.get_ticket(db, ticket_id)
    if ticket is None:
        raise HTTPException(status_code=404, detail=f"Ticket {ticket_id} not found")
    rag = SupportRAGService(rag_index, rag_settings)
    suggestion = rag.suggest(ticket.description, ticket_id=ticket_id)
    return schemas.SuggestionOut(
        abstained=suggestion.abstained,
        category=suggestion.category,
        confidence=suggestion.confidence,
        sources=[schemas.SuggestionSource(
            article_id=s.article_id, title=s.title, category=s.category, similarity=s.similarity,
        ) for s in suggestion.sources],
        draft_response=suggestion.draft_response,
        draft_source=suggestion.draft_source,
    )


@app.get("/tickets/{ticket_id}/related", response_model=list[schemas.RelatedTicketOut])
def get_related_tickets(
    ticket_id: int,
    db: Session = Depends(get_db),
    rag_index: RAGIndex = Depends(get_rag_index),
):
    """
    "Has this happened before?" - up to 3 similar past tickets, found by
    reusing the same embedder SupportRAG already builds (see
    app/related_tickets.py). Read-only, informational, no confirm step
    needed since nothing here is a decision the way a category is.
    """
    ticket = crud.get_ticket(db, ticket_id)
    if ticket is None:
        raise HTTPException(status_code=404, detail=f"Ticket {ticket_id} not found")
    candidates = crud.list_other_tickets(db, exclude_id=ticket_id)
    related = find_related_tickets(rag_index.embedder, ticket.description, candidates)
    return [
        schemas.RelatedTicketOut(id=r.id, description=r.description, status=r.status, similarity=r.similarity)
        for r in related
    ]


@app.get("/health")
def health_check(db: Session = Depends(get_db)):
    """
    Checks actual DB connectivity, not just "the process is running" -
    a health check that always returns ok regardless of DB state isn't
    a meaningful one in production.
    """
    try:
        db.execute(__import__("sqlalchemy").text("SELECT 1"))
        db_ok = True
    except Exception:
        logger.exception("Health check DB connectivity failure")
        db_ok = False
    return {"status": "ok" if db_ok else "degraded", "database": "ok" if db_ok else "unreachable"}


# --- Telephony (Milestone 3): Twilio webhooks ---
#
# A THIRD auth mechanism, alongside the JSON API's shared key and the
# browser UI's sessions - these routes are called directly by Twilio's
# servers, not a browser or an API client, so neither a session cookie
# nor an API key applies. Twilio's request signature (validated via
# app/telephony.py) is the entire security boundary here: every route
# below validates it FIRST, before touching any request data, and
# rejects with 403 on any failure - missing signature, wrong signature,
# or TWILIO_AUTH_TOKEN not configured at all (fails closed, not open).

async def _validate_twilio_request(request: Request, settings: Settings) -> dict:
    """
    Shared validation for every Twilio webhook route: reads the posted
    form data, validates the signature against it, and returns the
    form as a plain dict if valid. Raises HTTPException(403) otherwise -
    callers don't need their own try/except, just await this first.
    """
    form = await request.form()
    params = dict(form)
    signature = request.headers.get("X-Twilio-Signature", "")
    url = str(request.url)
    if not validate_twilio_signature(url, params, signature, settings.twilio_auth_token):
        logger.warning("Rejected Twilio webhook with invalid signature at %s", request.url.path)
        raise HTTPException(status_code=403, detail="Invalid Twilio signature")
    return params


@app.post("/webhooks/twilio/voice")
async def twilio_voice_webhook(request: Request, db: Session = Depends(get_db), settings: Settings = Depends(get_settings)):
    """
    Twilio calls this when a call comes in to our number. Logs the call
    (idempotently, keyed by CallSid), attempts caller-ID lookup, and
    responds with TwiML telling Twilio what to say/do. This app has no
    agent phone numbers to <Dial> to, so the response is deliberately
    simple: acknowledge the call and let the agent follow up via the
    ticket/customer record the call gets logged against.
    """
    params = await _validate_twilio_request(request, settings)

    crud.upsert_call_from_webhook(
        db,
        twilio_call_sid=params.get("CallSid", ""),
        direction="inbound",
        from_number=params.get("From", ""),
        to_number=params.get("To", ""),
        status=params.get("CallStatus", "ringing"),
    )

    twiml = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        "<Response><Say>Thanks for calling support. "
        "We've logged your call and an agent will follow up with you shortly.</Say></Response>"
    )
    return Response(content=twiml, media_type="application/xml")


@app.post("/webhooks/twilio/status")
async def twilio_status_webhook(request: Request, db: Session = Depends(get_db), settings: Settings = Depends(get_settings)):
    """
    Twilio calls this on every status change for a call (ringing ->
    in-progress -> completed, etc.), and again with the final outcome
    including duration and any recording URL. Twilio guarantees at-
    least-once delivery - crud.upsert_call_from_webhook is what makes
    receiving the same callback twice safe (updates the same row by
    CallSid, never creates a duplicate).
    """
    params = await _validate_twilio_request(request, settings)

    duration = params.get("CallDuration")
    crud.upsert_call_from_webhook(
        db,
        twilio_call_sid=params.get("CallSid", ""),
        direction=params.get("Direction", "inbound"),
        from_number=params.get("From", ""),
        to_number=params.get("To", ""),
        status=params.get("CallStatus", "ringing"),
        duration_seconds=int(duration) if duration and duration.isdigit() else None,
        recording_url=params.get("RecordingUrl"),
    )
    return Response(content="", status_code=200)


# --- Agent auth (Milestone 1): login, logout, invites ---
# Real session-based auth for human agents using the browser. See
# app/auth.py for why this is separate from the JSON API's shared key.

@app.get("/login")
def login_form(request: Request, next: Optional[str] = None):
    return templates.TemplateResponse(request, "login.html", {"next": next, "error": None})


@app.post("/login")
@limiter.limit(settings.login_rate_limit)
def login_submit(
    request: Request,
    email: str = Form(...),
    password: str = Form(...),
    next: Optional[str] = Form(default=None),
    db: Session = Depends(get_db),
):
    from app.security import verify_password

    user = crud.get_user_by_email(db, email.strip().lower())
    # Deliberately identical error for "no such user" and "wrong
    # password" - distinguishing them lets an attacker enumerate valid
    # emails, which is exactly what a generic message avoids.
    generic_error = "Incorrect email or password."
    if user is None or not user.is_active or not verify_password(password, user.password_hash):
        return templates.TemplateResponse(
            request, "login.html", {"next": next, "error": generic_error}, status_code=401,
        )

    session = crud.create_session(db, user.id, ttl_hours=settings.session_ttl_hours)
    response = RedirectResponse(url=next or "/", status_code=303)
    response.set_cookie(
        SESSION_COOKIE_NAME, session.id,
        httponly=True, samesite="lax", secure=settings.session_cookie_secure,
        max_age=settings.session_ttl_hours * 3600,
    )
    return response


@app.post("/logout")
def logout(request: Request, db: Session = Depends(get_db)):
    session_id = request.cookies.get(SESSION_COOKIE_NAME)
    if session_id:
        crud.revoke_session(db, session_id)
    response = RedirectResponse(url="/login", status_code=303)
    response.delete_cookie(SESSION_COOKIE_NAME)
    return response


@app.get("/invite")
def invite_form(request: Request, user: User = Depends(require_role(UserRole.admin)), settings: Settings = Depends(get_settings)):
    return templates.TemplateResponse(
        request, "invite.html",
        {"user": user, "csrf_token": csrf_token_for_template(request, settings), "invite_link": None, "error": None},
    )


@app.post("/invite")
def invite_submit(
    request: Request,
    email: str = Form(...),
    role: str = Form(...),
    csrf_token: str = Form(...),
    user: User = Depends(require_role(UserRole.admin)),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
):
    from app.security import verify_csrf_token
    session_id = request.cookies.get(SESSION_COOKIE_NAME)
    if not session_id or not verify_csrf_token(csrf_token, session_id, settings.secret_key):
        raise HTTPException(status_code=403, detail="Invalid or missing CSRF token. Reload the page and try again.")

    try:
        role_enum = UserRole(role)
    except ValueError:
        raise HTTPException(status_code=422, detail=f"Invalid role: {role}")

    invite = crud.create_invite(db, email=email.strip().lower(), role=role_enum, invited_by_user_id=user.id, ttl_hours=settings.invite_ttl_hours)
    invite_link = str(request.url_for("accept_invite_form")) + f"?token={invite.token}"
    return templates.TemplateResponse(
        request, "invite.html",
        {"user": user, "csrf_token": csrf_token_for_template(request, settings), "invite_link": invite_link, "error": None},
    )


@app.get("/accept-invite")
def accept_invite_form(request: Request, token: str, db: Session = Depends(get_db)):
    invite = crud.get_valid_invite(db, token)
    if invite is None:
        return templates.TemplateResponse(
            request, "accept_invite.html",
            {"token": token, "invite": None, "error": "This invite link is invalid, expired, or already used."},
            status_code=400,
        )
    return templates.TemplateResponse(request, "accept_invite.html", {"token": token, "invite": invite, "error": None})


@app.post("/accept-invite")
def accept_invite_submit(
    request: Request,
    token: str = Form(...),
    name: str = Form(...),
    password: str = Form(...),
    db: Session = Depends(get_db),
):
    invite = crud.get_valid_invite(db, token)
    if invite is None:
        return templates.TemplateResponse(
            request, "accept_invite.html",
            {"token": token, "invite": None, "error": "This invite link is invalid, expired, or already used."},
            status_code=400,
        )
    if len(password) < 8:
        return templates.TemplateResponse(
            request, "accept_invite.html",
            {"token": token, "invite": invite, "error": "Password must be at least 8 characters."},
            status_code=422,
        )
    if crud.get_user_by_email(db, invite.email) is not None:
        return templates.TemplateResponse(
            request, "accept_invite.html",
            {"token": token, "invite": None, "error": "An account with this email already exists."},
            status_code=409,
        )

    user = crud.create_user(db, email=invite.email, name=name, password=password, role=invite.role)
    crud.mark_invite_used(db, invite)
    session = crud.create_session(db, user.id, ttl_hours=settings.session_ttl_hours)
    response = RedirectResponse(url="/", status_code=303)
    response.set_cookie(
        SESSION_COOKIE_NAME, session.id,
        httponly=True, samesite="lax", secure=settings.session_cookie_secure,
        max_age=settings.session_ttl_hours * 3600,
    )
    return response


# --- Web UI routes (human agents, real session auth) ---
#
# These render HTML (Jinja2 templates) and handle browser form
# submissions, but call the exact same crud.py functions the JSON API
# and CLI use - no logic is duplicated, only the presentation differs.
# Reads require any logged-in agent (require_agent); writes require
# admin or agent role specifically (reviewer is read-only by design)
# AND a valid CSRF token (require_csrf), since these are cookie-
# authenticated state changes.

@app.get("/")
def ui_index(
    request: Request, status: Optional[str] = None, q: Optional[str] = None,
    db: Session = Depends(get_db), user: User = Depends(require_agent),
    settings: Settings = Depends(get_settings),
):
    tickets = crud.list_tickets(db, status=status, q=q, limit=settings.max_page_size)
    stats = crud.get_ticket_stats(db)
    return templates.TemplateResponse(
        request, "index.html",
        {
            "tickets": tickets, "current_status": status, "current_q": q, "stats": stats,
            "user": user, "csrf_token": csrf_token_for_template(request, settings),
        },
    )


@app.post("/ui/tickets", dependencies=[Depends(require_role(UserRole.admin, UserRole.agent)), Depends(require_csrf)])
def ui_create_ticket(request: Request, description: str = Form(...), db: Session = Depends(get_db), settings: Settings = Depends(get_settings)):
    try:
        validated = schemas.TicketCreate(description=description)
    except ValidationError as exc:
        error_messages = [err["msg"].removeprefix("Value error, ") for err in exc.errors()]
        tickets = crud.list_tickets(db)
        stats = crud.get_ticket_stats(db)
        return templates.TemplateResponse(
            request,
            "index.html",
            {
                "tickets": tickets,
                "current_status": None,
                "form_error": "; ".join(error_messages),
                "submitted_description": description,
                "stats": stats,
                "user": get_current_user(request, db),
                "csrf_token": csrf_token_for_template(request, settings),
            },
            status_code=422,
        )

    crud.create_ticket(db, validated.description)
    return RedirectResponse(url="/", status_code=303)


@app.get("/ui/tickets/{ticket_id}")
def ui_ticket_detail(
    request: Request, ticket_id: int,
    db: Session = Depends(get_db),
    rag_index: RAGIndex = Depends(get_rag_index),
    user: User = Depends(require_agent),
    settings: Settings = Depends(get_settings),
):
    ticket = crud.get_ticket(db, ticket_id)
    if ticket is None:
        raise HTTPException(status_code=404, detail=f"Ticket {ticket_id} not found")
    candidates = crud.list_other_tickets(db, exclude_id=ticket_id)
    related = find_related_tickets(rag_index.embedder, ticket.description, candidates)
    return templates.TemplateResponse(
        request, "ticket_detail.html",
        _ticket_detail_context(db, ticket, related, user, request, settings),
    )


def _ticket_detail_context(db: Session, ticket, related_tickets, user: User, request: Request, settings: Settings, suggestion=None, conflict_error: Optional[str] = None) -> dict:
    """Shared context-building for every route that renders
    ticket_detail.html, so each one doesn't have to remember every
    field (customer, assignee, agents list, audit trail, draft
    messages) individually."""
    customer = crud.get_customer(db, ticket.customer_id) if ticket.customer_id else None
    assignee = crud.get_user(db, ticket.assignee_id) if ticket.assignee_id else None
    agents = crud.list_agents(db)
    audit_events = crud.get_audit_events_for_ticket(db, ticket.id)
    audit_actors = {a.id: a for a in agents}
    for event in audit_events:
        if event.actor_user_id and event.actor_user_id not in audit_actors:
            actor = crud.get_user(db, event.actor_user_id)
            if actor:
                audit_actors[actor.id] = actor
    messages = crud.list_messages_for_ticket(db, ticket.id)
    message_jobs = {m.id: crud.get_outbox_job_for_message(db, m.id) for m in messages}
    return {
        "ticket": ticket, "related_tickets": related_tickets, "user": user,
        "csrf_token": csrf_token_for_template(request, settings),
        "customer": customer, "assignee": assignee, "agents": agents,
        "audit_events": audit_events, "audit_actors": audit_actors,
        "suggestion": suggestion, "conflict_error": conflict_error,
        "messages": messages, "message_jobs": message_jobs,
    }


@app.post("/ui/tickets/{ticket_id}/suggest", dependencies=[Depends(require_agent)])
@limiter.limit(settings.suggest_rate_limit)
def ui_suggest_category(
    request: Request, ticket_id: int,
    db: Session = Depends(get_db),
    rag_index: RAGIndex = Depends(get_rag_index),
    rag_settings: Settings = Depends(get_settings),
):
    ticket = crud.get_ticket(db, ticket_id)
    if ticket is None:
        raise HTTPException(status_code=404, detail=f"Ticket {ticket_id} not found")
    rag = SupportRAGService(rag_index, rag_settings)
    suggestion = rag.suggest(ticket.description, ticket_id=ticket_id)
    candidates = crud.list_other_tickets(db, exclude_id=ticket_id)
    related = find_related_tickets(rag_index.embedder, ticket.description, candidates)
    user = get_current_user(request, db)
    return templates.TemplateResponse(
        request, "ticket_detail.html",
        _ticket_detail_context(db, ticket, related, user, request, rag_settings, suggestion=suggestion),
    )


@app.post("/ui/tickets/{ticket_id}/status", dependencies=[Depends(require_csrf)])
def ui_update_status(
    request: Request, ticket_id: int, status: str = Form(...), version: int = Form(...),
    db: Session = Depends(get_db), user: User = Depends(require_role(UserRole.admin, UserRole.agent)),
    rag_index: RAGIndex = Depends(get_rag_index), settings: Settings = Depends(get_settings),
):
    try:
        ticket = crud.update_status(db, ticket_id, status, expected_version=version, actor_user_id=user.id)
    except crud.VersionConflict:
        return _render_conflict(request, db, ticket_id, rag_index, user, settings)
    if ticket is None:
        raise HTTPException(status_code=404, detail=f"Ticket {ticket_id} not found")
    return RedirectResponse(url=f"/ui/tickets/{ticket_id}", status_code=303)


@app.post("/ui/tickets/{ticket_id}/category", dependencies=[Depends(require_csrf)])
def ui_confirm_category(
    request: Request, ticket_id: int, category: str = Form(...), version: int = Form(...),
    db: Session = Depends(get_db), user: User = Depends(require_role(UserRole.admin, UserRole.agent)),
    rag_index: RAGIndex = Depends(get_rag_index), settings: Settings = Depends(get_settings),
):
    try:
        ticket = crud.confirm_category(db, ticket_id, category, expected_version=version, actor_user_id=user.id)
    except crud.VersionConflict:
        return _render_conflict(request, db, ticket_id, rag_index, user, settings)
    if ticket is None:
        raise HTTPException(status_code=404, detail=f"Ticket {ticket_id} not found")
    return RedirectResponse(url=f"/ui/tickets/{ticket_id}", status_code=303)


@app.post("/ui/tickets/{ticket_id}/assign", dependencies=[Depends(require_csrf)])
def ui_assign_ticket(
    request: Request, ticket_id: int, assignee_id: Optional[str] = Form(default=None), version: int = Form(...),
    db: Session = Depends(get_db), user: User = Depends(require_role(UserRole.admin, UserRole.agent)),
    rag_index: RAGIndex = Depends(get_rag_index), settings: Settings = Depends(get_settings),
):
    # assignee_id is deliberately optional, not required-but-sometimes-
    # empty: a blank/absent value means "unassign", and FastAPI/Starlette
    # treats an empty-string form field as MISSING for a required
    # Form(...) param (confirmed directly - it 422s with "Field
    # required" rather than delivering ""), so making it required would
    # make unassigning impossible through this route.
    parsed_assignee_id = int(assignee_id) if assignee_id else None
    try:
        ticket = crud.assign_ticket(db, ticket_id, parsed_assignee_id, expected_version=version, actor_user_id=user.id)
    except crud.VersionConflict:
        return _render_conflict(request, db, ticket_id, rag_index, user, settings)
    if ticket is None:
        raise HTTPException(status_code=404, detail=f"Ticket {ticket_id} not found")
    return RedirectResponse(url=f"/ui/tickets/{ticket_id}", status_code=303)


@app.post("/ui/tickets/{ticket_id}/customer", dependencies=[Depends(require_csrf)])
def ui_link_customer(
    request: Request, ticket_id: int, customer_id: int = Form(...), version: int = Form(...),
    db: Session = Depends(get_db), user: User = Depends(require_role(UserRole.admin, UserRole.agent)),
    rag_index: RAGIndex = Depends(get_rag_index), settings: Settings = Depends(get_settings),
):
    if crud.get_customer(db, customer_id) is None:
        raise HTTPException(status_code=404, detail=f"Customer {customer_id} not found")
    try:
        ticket = crud.link_ticket_to_customer(db, ticket_id, customer_id, expected_version=version, actor_user_id=user.id)
    except crud.VersionConflict:
        return _render_conflict(request, db, ticket_id, rag_index, user, settings)
    if ticket is None:
        raise HTTPException(status_code=404, detail=f"Ticket {ticket_id} not found")
    return RedirectResponse(url=f"/ui/tickets/{ticket_id}", status_code=303)


def _render_conflict(request: Request, db: Session, ticket_id: int, rag_index: RAGIndex, user: User, settings: Settings):
    """
    Shared handling for a VersionConflict on any ticket write: re-render
    the ticket page with the ticket's REAL current data (so the form the
    agent sees is no longer stale) and a clear explanation of what
    happened, rather than a generic error page or - worse - retrying
    the write blind.
    """
    ticket = crud.get_ticket(db, ticket_id)
    candidates = crud.list_other_tickets(db, exclude_id=ticket_id)
    related = find_related_tickets(rag_index.embedder, ticket.description, candidates)
    context = _ticket_detail_context(
        db, ticket, related, user, request, settings,
        conflict_error="This ticket changed since you loaded the page (someone else updated it, or you had it open in another tab). Review the current state below and try again.",
    )
    return templates.TemplateResponse(request, "ticket_detail.html", context, status_code=409)


# --- Outbound messages (Milestone 2: draft, approve, send) ---
#
# Same shape as every other ticket write: admin/agent only, CSRF-
# protected, version-checked. Approval additionally snapshots the
# content and enqueues the outbox job - see crud.approve_message.

@app.post("/ui/tickets/{ticket_id}/messages", dependencies=[Depends(require_role(UserRole.admin, UserRole.agent)), Depends(require_csrf)])
def ui_create_draft_message(
    request: Request, ticket_id: int,
    recipient_email: str = Form(...), subject: str = Form(...), body: str = Form(...),
    db: Session = Depends(get_db), user: User = Depends(require_role(UserRole.admin, UserRole.agent)),
):
    if crud.get_ticket(db, ticket_id) is None:
        raise HTTPException(status_code=404, detail=f"Ticket {ticket_id} not found")
    crud.create_draft(db, ticket_id, recipient_email.strip(), subject.strip(), body, created_by_user_id=user.id)
    return RedirectResponse(url=f"/ui/tickets/{ticket_id}", status_code=303)


@app.post("/ui/tickets/{ticket_id}/messages/{message_id}", dependencies=[Depends(require_csrf)])
def ui_update_draft_message(
    request: Request, ticket_id: int, message_id: int,
    recipient_email: str = Form(...), subject: str = Form(...), body: str = Form(...), version: int = Form(...),
    db: Session = Depends(get_db), user: User = Depends(require_role(UserRole.admin, UserRole.agent)),
    rag_index: RAGIndex = Depends(get_rag_index), settings: Settings = Depends(get_settings),
):
    try:
        message = crud.update_draft(db, message_id, recipient_email.strip(), subject.strip(), body, expected_version=version)
    except crud.VersionConflict:
        return _render_conflict(request, db, ticket_id, rag_index, user, settings)
    except crud.MessageNotDraft:
        return _render_conflict(request, db, ticket_id, rag_index, user, settings)
    if message is None:
        raise HTTPException(status_code=404, detail=f"Message {message_id} not found")
    return RedirectResponse(url=f"/ui/tickets/{ticket_id}", status_code=303)


@app.post("/ui/tickets/{ticket_id}/messages/{message_id}/approve", dependencies=[Depends(require_csrf)])
def ui_approve_message(
    request: Request, ticket_id: int, message_id: int, version: int = Form(...),
    db: Session = Depends(get_db), user: User = Depends(require_role(UserRole.admin, UserRole.agent)),
    rag_index: RAGIndex = Depends(get_rag_index), settings: Settings = Depends(get_settings),
):
    """
    Approves a message: snapshots its exact content, and atomically
    queues it for sending (see crud.approve_message). This is the
    agent-confirmation gate the brief requires per outgoing message -
    nothing else in the system can queue a send without this exact,
    authenticated, version-checked call happening first.
    """
    try:
        message = crud.approve_message(db, message_id, actor_user_id=user.id, expected_version=version)
    except crud.VersionConflict:
        return _render_conflict(request, db, ticket_id, rag_index, user, settings)
    except crud.MessageNotDraft:
        return _render_conflict(request, db, ticket_id, rag_index, user, settings)
    if message is None:
        raise HTTPException(status_code=404, detail=f"Message {message_id} not found")
    return RedirectResponse(url=f"/ui/tickets/{ticket_id}", status_code=303)


@app.get("/calls")
def ui_calls_list(request: Request, db: Session = Depends(get_db), user: User = Depends(require_agent)):
    """The call log: every call Twilio has told us about, most recent
    first. Matched calls link to their contact; unmatched ones show the
    raw number for an agent to manually reconcile."""
    calls = crud.list_recent_calls(db)
    contacts_by_id = {}
    for call in calls:
        if call.contact_id and call.contact_id not in contacts_by_id:
            contact = crud.get_contact(db, call.contact_id)
            if contact:
                contacts_by_id[call.contact_id] = contact
    return templates.TemplateResponse(
        request, "calls.html", {"calls": calls, "contacts_by_id": contacts_by_id, "user": user},
    )


# --- Customers (Milestone 1: exact customer history, separate from
# semantic related tickets - see crud.get_customer_tickets's docstring) ---

@app.get("/customers")
def ui_customers_list(request: Request, q: Optional[str] = None, db: Session = Depends(get_db), user: User = Depends(require_agent), settings: Settings = Depends(get_settings)):
    customers = crud.search_customers(db, q=q)
    return templates.TemplateResponse(
        request, "customers.html",
        {"customers": customers, "current_q": q, "user": user, "csrf_token": csrf_token_for_template(request, settings)},
    )


@app.post("/customers", dependencies=[Depends(require_role(UserRole.admin, UserRole.agent)), Depends(require_csrf)])
def ui_create_customer(request: Request, name: str = Form(...), db: Session = Depends(get_db)):
    customer = crud.create_customer(db, name=name)
    return RedirectResponse(url=f"/customers/{customer.id}", status_code=303)


@app.get("/customers/{customer_id}")
def ui_customer_detail(request: Request, customer_id: int, db: Session = Depends(get_db), user: User = Depends(require_agent), settings: Settings = Depends(get_settings)):
    customer = crud.get_customer(db, customer_id)
    if customer is None:
        raise HTTPException(status_code=404, detail=f"Customer {customer_id} not found")
    contacts = crud.list_contacts_for_customer(db, customer_id)
    tickets = crud.get_customer_tickets(db, customer_id)
    calls = crud.list_calls_for_customer(db, customer_id)
    return templates.TemplateResponse(
        request, "customer_detail.html",
        {
            "customer": customer, "contacts": contacts, "tickets": tickets, "calls": calls,
            "user": user, "csrf_token": csrf_token_for_template(request, settings),
        },
    )


@app.post("/customers/{customer_id}/contacts", dependencies=[Depends(require_role(UserRole.admin, UserRole.agent)), Depends(require_csrf)])
def ui_create_contact(request: Request, customer_id: int, name: str = Form(...), email: str = Form(default=""), phone: str = Form(default=""), db: Session = Depends(get_db)):
    if crud.get_customer(db, customer_id) is None:
        raise HTTPException(status_code=404, detail=f"Customer {customer_id} not found")
    crud.create_contact(db, customer_id=customer_id, name=name, email=email or None, phone=phone or None)
    return RedirectResponse(url=f"/customers/{customer_id}", status_code=303)
