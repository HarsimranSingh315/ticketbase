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
    require_api_key, require_agent, require_role, require_csrf, require_session_or_api_key, require_session_csrf_or_api_key,
    get_current_user, csrf_token_for_template, AuthRedirect, SESSION_COOKIE_NAME,
)
from app.models import User, UserRole
from app.related_tickets import find_related_tickets
from app.supportrag import SupportRAGService, RAGIndex, build_rag_index, seed_knowledge_base, IndexBuildError, Embedder as _Embedder
from app.telephony import validate_twilio_signature, get_call_adapter
from app.security import generate_token
from app.validation import ValidationError

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


def load_initial_index(state, db: Session) -> None:
    """Startup index load. B4 safe-unavailable state: if the persisted KB
    can't be indexed, serve with an empty index (every suggestion
    abstains) rather than crash - tickets, customers and email keep
    working, and /ready reports suggestions as degraded."""
    state.rag_index_error = None
    try:
        state.rag_index = build_rag_index(db)
    except IndexBuildError as exc:
        db.rollback()
        logger.error("Knowledge base could not be indexed at startup; suggestions disabled: %s", exc)
        state.rag_index = RAGIndex(embedder=_Embedder(), articles=[])
        state.rag_index_error = str(exc)
    state.kb_revision = crud.get_kb_revision(db)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Schema auto-create is a development convenience only. In strict
    # environments the schema must come exclusively from Alembic (run as
    # the Render pre-deploy step) - create_all there could silently
    # create tables that no migration knows about, masking a missed
    # migration until it fails in a confusing way later.
    from app.config import STRICT_ENVIRONMENTS
    if settings.environment not in STRICT_ENVIRONMENTS:
        Base.metadata.create_all(bind=engine)

    db = SessionLocal()
    try:
        load_initial_index(app.state, db)
        _bootstrap_admin_if_needed(db)
    finally:
        db.close()

    logger.info("TicketBase startup complete (db=%s)", settings.database_url.split("://")[0])
    yield
    logger.info("TicketBase shutting down")


limiter = Limiter(key_func=get_remote_address)

app = FastAPI(
    title="TicketBase", version="0.3.0", lifespan=lifespan,
    # See Settings.enable_api_docs's docstring - off by default so the
    # full API surface isn't published to the open internet by default.
    docs_url="/docs" if settings.enable_api_docs else None,
    redoc_url="/redoc" if settings.enable_api_docs else None,
    openapi_url="/openapi.json" if settings.enable_api_docs else None,
)


@app.middleware("http")
async def security_headers_middleware(request: Request, call_next):
    """
    Standard defense-in-depth headers on every response - cheap,
    expected, and previously entirely missing. Content-Security-Policy
    is scoped to what this app's templates actually load (checked
    directly, not guessed): Google Fonts (style/font), a single
    self-hosted script (app.js), and data: URIs for the inline SVG
    favicon. style-src allows 'unsafe-inline' because the templates use
    inline style="..." attributes throughout - a real, deliberate
    tradeoff (refactoring every one into a CSS class to tighten this
    further was judged not worth the churn/regression risk for a
    headers pass), not an oversight.
    """
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Strict-Transport-Security"] = "max-age=63072000; includeSubDomains"
    # Invite links carry a bearer token in the query string - send no
    # referrer at all from those pages so it can't leak onward.
    response.headers["Referrer-Policy"] = "no-referrer" if request.url.path.startswith("/accept-invite") else "same-origin"
    # Authenticated pages and JSON contain customer data. Only versioned
    # static assets may be cached; everything else must not be stored by
    # browsers or shared proxies (review: missing no-store on sensitive
    # responses).
    if not request.url.path.startswith("/static/"):
        response.headers["Cache-Control"] = "no-store"
        response.headers["Pragma"] = "no-cache"
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; "
        "script-src 'self'; "
        "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
        "font-src https://fonts.gstatic.com; "
        "img-src 'self' data:; "
        "object-src 'none'; "
        "base-uri 'self'; "
        "form-action 'self'; "
        "frame-ancestors 'none';"
    )
    return response
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)
app.mount("/static", StaticFiles(directory="app/static"), name="static")
def _global_template_context(request: Request) -> dict:
    """
    Makes csrf_token available in EVERY template render automatically,
    not just the ones that remember to pass it explicitly - the logout
    form lives in base.html, rendered on every authenticated page, so
    it needs a valid token regardless of which specific route rendered
    the page. A route that explicitly passes its own csrf_token (most
    of them do, for their own forms) simply overrides this default -
    Starlette merges context processor output first, then the route's
    own context on top, so nothing here changes existing behavior.
    Returns None when there's no session (see csrf_token_for_template) -
    safe on pages like /login where there's genuinely nothing to bind
    a post-login CSRF token to yet.
    """
    # Resolve settings exactly as routes do (honouring dependency
    # overrides), so a rendered token is always signed with the same
    # secret the verifying route uses.
    settings_provider = request.app.dependency_overrides.get(get_settings, get_settings)
    return {"csrf_token": csrf_token_for_template(request, settings_provider())}


templates = Jinja2Templates(directory="app/templates", context_processors=[_global_template_context])


@app.exception_handler(AuthRedirect)
async def auth_redirect_handler(request: Request, exc: AuthRedirect):
    return RedirectResponse(url=f"/login?next={exc.next_path}", status_code=303)


@app.exception_handler(ValidationError)
async def validation_error_handler(request: Request, exc: ValidationError):
    """Any input problem that reaches CRUD without a route-specific
    handler becomes a 422 with the field named - never a 500."""
    from fastapi.responses import JSONResponse
    return JSONResponse(status_code=422, content={"detail": exc.message, "field": exc.field})


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


def _kb_form_error(request: Request, user: User, settings: Settings, article, form: dict, message: str, status_code: int = 422):
    """Re-render the KB form with the agent's own submitted text intact."""
    return templates.TemplateResponse(request, "kb_edit.html", {
        "article": article, "user": user, "csrf_token": csrf_token_for_template(request, settings),
        "error": message, "form": form,
    }, status_code=status_code)


class KBChangeRejected(Exception):
    """A KB edit would leave the knowledge base un-indexable."""


def _apply_kb_change(request: Request, db: Session, change):
    """
    B4: validate BEFORE commit. Runs `change(db)` without committing,
    builds a candidate index from that pending state, and only if it
    succeeds commits the change, its embeddings and a revision bump in ONE
    transaction, then publishes the new index. On failure everything is
    rolled back and the last-known-good index stays in service - a bad
    edit can no longer persist content that breaks every later index
    build, including at startup.

    B3: the revision bump lets OTHER processes notice (see get_rag_index).
    """
    result = change(db)
    try:
        candidate = build_rag_index(db, persist=True, seed=False, commit=False)
    except IndexBuildError as exc:
        db.rollback()
        raise KBChangeRejected(str(exc)) from exc
    revision = crud.bump_kb_revision(db)
    db.commit()
    request.app.state.rag_index = candidate
    request.app.state.kb_revision = revision
    return result


def get_rag_index(request: Request, db: Session = Depends(get_db)) -> RAGIndex:
    """
    Returns this process's cached index, first checking one small row
    (kb_state.revision) to see whether another process changed the KB
    since this index was built (B3). If so it rebuilds - read-only, no
    writes - and on failure keeps serving the last-known-good index.
    """
    state = request.app.state
    try:
        current = crud.get_kb_revision(db)
    except Exception:
        logger.exception("KB revision check failed; serving cached index")
        db.rollback()
        return state.rag_index
    if current != getattr(state, "kb_revision", None):
        try:
            state.rag_index = build_rag_index(db, persist=False, seed=False)
            state.rag_index_error = None
        except IndexBuildError as exc:
            logger.error("KB revision %s could not be indexed; keeping previous index: %s", current, exc)
            state.rag_index_error = str(exc)
        state.kb_revision = current  # on failure too: don't retry the build on every request
    return state.rag_index


# --- JSON API (machine clients: CLI, scripts, integrations) ---
# Protected by the shared API key (app/auth.py's require_api_key),
# off by default for local dev/tests.

@app.post("/tickets", response_model=schemas.TicketOut, status_code=201, dependencies=[Depends(require_api_key)])
def create_ticket(payload: schemas.TicketCreate, db: Session = Depends(get_db)):
    ticket = crud.create_ticket(db, payload.description)
    return ticket


@app.get("/tickets", response_model=list[schemas.TicketOut], dependencies=[Depends(require_session_or_api_key)])
def list_tickets(
    status: Optional[str] = None,
    priority: Optional[str] = None,
    q: Optional[str] = None,
    limit: int = Query(default=settings.default_page_size, ge=1, le=settings.max_page_size),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
):
    return crud.list_tickets(db, status=status, priority=priority, q=q, limit=limit, offset=offset)


@app.get("/tickets/{ticket_id}", response_model=schemas.TicketOut, dependencies=[Depends(require_session_or_api_key)])
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


@app.post("/tickets/{ticket_id}/suggest", response_model=schemas.SuggestionOut, dependencies=[Depends(require_session_csrf_or_api_key)])
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


@app.get("/tickets/{ticket_id}/related", response_model=list[schemas.RelatedTicketOut], dependencies=[Depends(require_session_or_api_key)])
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


def _expected_migration_head() -> Optional[str]:
    """The newest Alembic revision in this build, read once. None if the
    migration scripts aren't present (e.g. a trimmed image)."""
    try:
        from alembic.config import Config
        from alembic.script import ScriptDirectory
        return ScriptDirectory.from_config(Config("alembic.ini")).get_current_head()
    except Exception:
        logger.warning("Could not determine expected migration head")
        return None


_MIGRATION_HEAD = _expected_migration_head()


@app.get("/live")
def liveness():
    """Liveness: the process is running and serving. Deliberately touches
    nothing external - a database outage should make the instance NOT
    READY, not get it restarted in a loop."""
    return {"status": "alive"}


@app.get("/ready")
def readiness(request: Request, db: Session = Depends(get_db)):
    """
    Readiness: can this instance serve real requests right now? Returns
    503 on failure - the previous /health returned HTTP 200 with a
    "degraded" body even when the database was unreachable, and a
    platform health check only reads the status code.

    Checks: database connectivity; schema at the migration head this
    build expects (production no longer auto-creates tables, so a
    skipped migration must show up here); retrieval index loaded.
    Outbox backlog is reported for visibility but doesn't fail readiness -
    the worker is optional in this deployment (HUMAN_TASKS H4).
    """
    from sqlalchemy import text
    from fastapi.responses import JSONResponse
    checks, ok = {}, True
    try:
        db.execute(text("SELECT 1"))
        checks["database"] = "ok"
    except Exception:
        logger.exception("Readiness: database unreachable")
        checks["database"] = "unreachable"
        ok = False

    if ok and _MIGRATION_HEAD:
        try:
            current = db.execute(text("SELECT version_num FROM alembic_version")).scalar()
        except Exception:
            current = None
            db.rollback()
        if current == _MIGRATION_HEAD:
            checks["schema"] = "ok"
        elif settings.environment in ("development", "test"):
            checks["schema"] = "unmanaged (development)"
        else:
            checks["schema"] = f"not at migration head (db={current}, expected={_MIGRATION_HEAD})"
            ok = False

    # Resolve the index through the same provider routes use (honouring
    # overrides), not app.state directly.
    provider = request.app.dependency_overrides.get(get_rag_index, get_rag_index)
    try:
        index_ok = (provider(request, db) if provider is get_rag_index else provider()) is not None
    except Exception:
        index_ok = False
    # Suggestions are assistive: an unindexable KB degrades them (they
    # abstain) but must not take ticket handling offline, so it's
    # reported here without failing readiness.
    if not index_ok:
        checks["retrieval_index"] = "missing"
        ok = False
    elif getattr(request.app.state, "rag_index_error", None):
        checks["retrieval_index"] = "degraded - suggestions disabled until the knowledge base is fixed"
    else:
        checks["retrieval_index"] = "ok"

    if checks["database"] == "ok":
        try:
            checks["outbox"] = crud.outbox_backlog(db)
        except Exception:
            db.rollback()
            checks["outbox"] = "unavailable"

    return JSONResponse(status_code=200 if ok else 503, content={"status": "ready" if ok else "not_ready", "checks": checks})


@app.get("/health")
def health_check(request: Request, db: Session = Depends(get_db)):
    """Kept for existing health-check configuration; same semantics as /ready."""
    return readiness(request, db)


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

PRELOGIN_CSRF_COOKIE_NAME = "prelogin_csrf"


def _set_auth_cookie(response, name: str, value: str, max_age: int) -> None:
    """Every auth-related cookie is set through here so attributes can't
    drift between call sites. Secure comes from settings, which strict
    environments force to True (config.validate_settings) - the
    pre-login cookie previously never set Secure at all."""
    response.set_cookie(
        name, value, max_age=max_age, path="/",
        httponly=True, samesite="lax", secure=get_settings().session_cookie_secure,
    )


def _delete_auth_cookie(response, name: str) -> None:
    """Deletion must repeat path/secure/samesite, or some browsers treat
    it as a different cookie and keep the original."""
    response.delete_cookie(
        name, path="/", httponly=True, samesite="lax", secure=get_settings().session_cookie_secure,
    )


@app.get("/login")
def login_form(request: Request, next: Optional[str] = None):
    """
    Login needs its OWN CSRF mechanism, distinct from every other form
    in the app: the existing synchronizer-token pattern
    (compute_csrf_token) derives its token FROM a session ID, and an
    anonymous visitor here has no session yet - there's nothing to
    derive from. This uses the double-submit-cookie pattern instead: a
    random value is set as a cookie AND embedded in the form; on
    submit, the two are compared. An attacker's cross-site page can't
    read the victim's cookie (same-origin policy) or guess its random
    value, so it can't construct a forged request where both match -
    flagged by an external review as a real, previously-missing gap on
    this specific route.
    """
    existing_token = request.cookies.get(PRELOGIN_CSRF_COOKIE_NAME)
    prelogin_token = existing_token or generate_token()
    response = templates.TemplateResponse(request, "login.html", {"next": next, "error": None, "prelogin_csrf": prelogin_token})
    if not existing_token:
        # Not set httponly=False for JS access - the token is embedded
        # server-side into the rendered form directly, so the cookie
        # never needs to be read by client-side script at all.
        _set_auth_cookie(response, PRELOGIN_CSRF_COOKIE_NAME, prelogin_token, max_age=3600)
    return response


def _is_safe_redirect_path(path: Optional[str]) -> bool:
    """
    Only a genuine same-origin, relative application path is safe to
    redirect to after login - anything else is a potential open
    redirect (an external review found `next` was passed straight to
    RedirectResponse with no check at all). Rejects: empty/missing,
    anything not starting with a single `/` (catches absolute URLs and
    scheme-based attacks like `javascript:`), and scheme-relative
    forms (`//evil.com`, `/\\evil.com` - some browsers normalize a
    leading backslash to a forward slash, a known bypass for a naive
    `//` check alone).
    """
    if not path:
        return False
    if not path.startswith("/"):
        return False
    if path.startswith("//") or path.startswith("/\\"):
        return False
    return True


# A real Argon2id hash of a random throwaway value, computed once per
# process, used to equalize login timing for unknown accounts.
from app.security import hash_password as _hash_password
_DUMMY_PASSWORD_HASH = _hash_password(generate_token())


@app.post("/login")
@limiter.limit(settings.login_rate_limit)
def login_submit(
    request: Request,
    email: str = Form(...),
    password: str = Form(...),
    next: Optional[str] = Form(default=None),
    prelogin_csrf: str = Form(default=""),
    db: Session = Depends(get_db),
):
    from app.security import verify_password
    import hmac as _hmac

    cookie_token = request.cookies.get(PRELOGIN_CSRF_COOKIE_NAME, "")
    if not cookie_token or not prelogin_csrf or not _hmac.compare_digest(cookie_token, prelogin_csrf):
        raise HTTPException(status_code=403, detail="Invalid or expired form. Reload the login page and try again.")

    user = crud.get_user_by_email(db, email.strip().lower())
    # Deliberately identical error for "no such user" and "wrong
    # password" - distinguishing them lets an attacker enumerate valid
    # emails, which is exactly what a generic message avoids.
    generic_error = "Incorrect email or password."
    # Always run exactly one Argon2 verification, even for unknown or
    # inactive accounts. Skipping it for nonexistent emails made those
    # responses measurably faster - a timing oracle for which emails
    # have accounts, despite the identical error text.
    password_ok = verify_password(password, user.password_hash if user is not None else _DUMMY_PASSWORD_HASH)
    if user is None or not user.is_active or not password_ok:
        return templates.TemplateResponse(
            request, "login.html", {"next": next, "error": generic_error, "prelogin_csrf": prelogin_csrf}, status_code=401,
        )

    raw_token, session = crud.create_session(db, user.id, ttl_hours=settings.session_ttl_hours)
    safe_next = next if _is_safe_redirect_path(next) else "/"
    response = RedirectResponse(url=safe_next, status_code=303)
    _set_auth_cookie(response, SESSION_COOKIE_NAME, raw_token, max_age=settings.session_ttl_hours * 3600)
    _delete_auth_cookie(response, PRELOGIN_CSRF_COOKIE_NAME)
    return response


@app.post("/logout")
def logout(request: Request, csrf_token: str = Form(...), db: Session = Depends(get_db), settings: Settings = Depends(get_settings)):
    from app.security import verify_csrf_token
    session_id = request.cookies.get(SESSION_COOKIE_NAME)
    # Verified BEFORE revoking anything - a forged cross-site logout is
    # a nuisance, not a severe risk, but there's no reason to skip the
    # same protection every other authenticated POST route already has
    # (an external review flagged this specific gap by name).
    if not session_id or not verify_csrf_token(csrf_token, session_id, settings.secret_key):
        raise HTTPException(status_code=403, detail="Invalid or missing CSRF token. Reload the page and try again.")
    crud.revoke_session(db, session_id)
    response = RedirectResponse(url="/login", status_code=303)
    _delete_auth_cookie(response, SESSION_COOKIE_NAME)
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

    raw_token, invite = crud.create_invite(db, email=email.strip().lower(), role=role_enum, invited_by_user_id=user.id, ttl_hours=settings.invite_ttl_hours)
    invite_link = str(request.url_for("accept_invite_form")) + f"?token={raw_token}"
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
    raw_token, session = crud.create_session(db, user.id, ttl_hours=settings.session_ttl_hours)
    response = RedirectResponse(url="/", status_code=303)
    _set_auth_cookie(response, SESSION_COOKIE_NAME, raw_token, max_age=settings.session_ttl_hours * 3600)
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
    request: Request, status: Optional[str] = None, q: Optional[str] = None, sla: Optional[str] = None,
    db: Session = Depends(get_db), user: User = Depends(require_agent),
    settings: Settings = Depends(get_settings),
):
    sla_hours = {"high": settings.sla_high_priority_hours, "medium": settings.sla_medium_priority_hours, "low": settings.sla_low_priority_hours}

    if sla == "breached":
        # A computed view, not a status filter - overdue tickets across
        # every non-resolved status, most overdue first. Ignores the
        # status/q filters deliberately: "what needs attention right
        # now" is a different question than "show me open tickets".
        tickets = crud.list_breached_tickets(db, sla_hours)
        breached_ids = {t.id for t in tickets}
    else:
        tickets = crud.list_tickets(db, status=status, q=q, limit=settings.max_page_size)
        breached_ids = {t.id for t in crud.list_breached_tickets(db, sla_hours)}

    stats = crud.get_ticket_stats(db)
    return templates.TemplateResponse(
        request, "index.html",
        {
            "tickets": tickets, "current_status": status, "current_q": q, "current_sla": sla,
            "stats": stats, "breached_ids": breached_ids,
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
        sla_hours = {"high": settings.sla_high_priority_hours, "medium": settings.sla_medium_priority_hours, "low": settings.sla_low_priority_hours}
        breached_ids = {t.id for t in crud.list_breached_tickets(db, sla_hours)}
        return templates.TemplateResponse(
            request,
            "index.html",
            {
                "tickets": tickets,
                "current_status": None,
                "current_sla": None,
                "breached_ids": breached_ids,
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
    contacts = crud.list_contacts_for_customer(db, customer.id) if customer else []
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
    sla_hours = {"high": settings.sla_high_priority_hours, "medium": settings.sla_medium_priority_hours, "low": settings.sla_low_priority_hours}
    sla_deadline = crud.compute_sla_deadline(ticket, sla_hours)
    sla_breached = crud.is_ticket_breached(ticket, sla_hours)
    calls = crud.list_calls_for_ticket(db, ticket.id)
    return {
        "ticket": ticket, "related_tickets": related_tickets, "user": user,
        "csrf_token": csrf_token_for_template(request, settings),
        "customer": customer, "contacts": contacts, "assignee": assignee, "agents": agents,
        "audit_events": audit_events, "audit_actors": audit_actors,
        "suggestion": suggestion, "conflict_error": conflict_error,
        "messages": messages, "message_jobs": message_jobs,
        "sla_deadline": sla_deadline, "sla_breached": sla_breached,
        "calls": calls,
    }


@app.post("/ui/tickets/{ticket_id}/suggest", dependencies=[Depends(require_agent), Depends(require_csrf)])
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


def _render_conflict(request: Request, db: Session, ticket_id: int, rag_index: RAGIndex, user: User, settings: Settings):
    """
    Shared handling for a VersionConflict on any ticket write: re-render
    the ticket page with the ticket's REAL current data (so the form the
    agent sees is no longer stale) and a clear explanation of what
    happened, rather than a generic error page or - worse - retrying
    the write blind.

    RESTORED after being found accidentally deleted entirely: caught by
    running the full test suite together (not just the new tests in
    isolation) before committing, which is exactly why that step is
    never skipped in this project even when the new feature's own
    tests all pass on their own.
    """
    return _render_ticket_error(
        request, db, ticket_id, rag_index, user, settings,
        "This ticket changed since you loaded the page (someone else updated it, or you had it open in another tab). Review the current state below and try again.",
        status_code=409,
    )


def _render_ticket_error(request: Request, db: Session, ticket_id: int, rag_index: RAGIndex, user: User,
                         settings: Settings, message: str, status_code: int = 422):
    """Re-renders the ticket page with a visible error instead of a bare
    error response, so the agent stays in context."""
    ticket = crud.get_ticket(db, ticket_id)
    if ticket is None:
        raise HTTPException(status_code=404, detail=f"Ticket {ticket_id} not found")
    candidates = crud.list_other_tickets(db, exclude_id=ticket_id)
    related = find_related_tickets(rag_index.embedder, ticket.description, candidates)
    context = _ticket_detail_context(db, ticket, related, user, request, settings, conflict_error=message)
    return templates.TemplateResponse(request, "ticket_detail.html", context, status_code=status_code)


@app.post("/ui/tickets/{ticket_id}/status", dependencies=[Depends(require_csrf)])
def ui_update_status(
    request: Request, ticket_id: int, status: str = Form(...), version: int = Form(...),
    db: Session = Depends(get_db), user: User = Depends(require_role(UserRole.admin, UserRole.agent)),
    rag_index: RAGIndex = Depends(get_rag_index), settings: Settings = Depends(get_settings),
):
    try:
        ticket = crud.update_status(db, ticket_id, status, expected_version=version, actor_user_id=user.id)
    except ValidationError as e:
        return _render_ticket_error(request, db, ticket_id, rag_index, user, settings, f"Couldn't update status - {e.message}.")
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
    except ValidationError as e:
        return _render_ticket_error(request, db, ticket_id, rag_index, user, settings, f"Couldn't set category - {e.message}.")
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


@app.post("/ui/tickets/{ticket_id}/call", dependencies=[Depends(require_role(UserRole.admin, UserRole.agent)), Depends(require_csrf)])
def ui_place_outbound_call(
    request: Request, ticket_id: int, contact_id: int = Form(...),
    db: Session = Depends(get_db), user: User = Depends(require_role(UserRole.admin, UserRole.agent)),
    rag_index: RAGIndex = Depends(get_rag_index), settings: Settings = Depends(get_settings),
):
    """
    Places an outbound call to a contact and records it - the
    REST-API-calling counterpart to the webhook-receiving side already
    built. Uses LocalSinkCallAdapter (records the attempt, no real call)
    unless TWILIO_ACCOUNT_SID/AUTH_TOKEN are both set - see
    app/telephony.py.
    """
    ticket = crud.get_ticket(db, ticket_id)
    if ticket is None:
        raise HTTPException(status_code=404, detail=f"Ticket {ticket_id} not found")
    contact = crud.get_contact(db, contact_id)
    if contact is None or not contact.phone:
        raise HTTPException(status_code=404, detail="Contact not found or has no phone number on file")
    # Confirms the contact actually belongs to the customer this ticket
    # is linked to - an external review found this wasn't checked at
    # all, meaning any valid contact_id (any customer's contact,
    # anywhere in the system) could be dialed from any ticket's call
    # button, regardless of whether it had anything to do with that
    # ticket's actual customer. A ticket with no customer linked yet
    # has no valid relationship to check against, so it's rejected too
    # - not a special "anything goes" case.
    if ticket.customer_id is None or contact.customer_id != ticket.customer_id:
        raise HTTPException(status_code=404, detail="This contact is not associated with this ticket's customer")

    if not settings.twilio_phone_number:
        return _render_call_error(
            request, db, ticket, rag_index, user, settings,
            "TWILIO_PHONE_NUMBER is not configured - set it in .env before placing outbound calls (even simulated local ones).",
        )

    adapter = get_call_adapter(settings)
    twiml_url = f"{settings.public_base_url}/webhooks/twilio/voice" if settings.public_base_url else None
    result = adapter.place_call(to_number=contact.phone, from_number=settings.twilio_phone_number, twiml_url=twiml_url)

    if not result.success:
        return _render_call_error(request, db, ticket, rag_index, user, settings, result.error or "The call could not be placed.")

    crud.create_outbound_call(
        db, call_sid=result.call_sid, to_number=contact.phone, from_number=settings.twilio_phone_number,
        contact_id=contact.id, ticket_id=ticket_id,
    )
    return RedirectResponse(url=f"/ui/tickets/{ticket_id}", status_code=303)


def _render_call_error(request: Request, db: Session, ticket, rag_index: RAGIndex, user: User, settings: Settings, error_message: str):
    """Same principle as _render_conflict: a failed call attempt gets a
    clear, specific error shown on the real current page, not a raw
    500 or a silently-swallowed failure."""
    candidates = crud.list_other_tickets(db, exclude_id=ticket.id)
    related = find_related_tickets(rag_index.embedder, ticket.description, candidates)
    context = _ticket_detail_context(
        db, ticket, related, user, request, settings,
        conflict_error=f"Couldn't place the call: {error_message}",
    )
    return templates.TemplateResponse(request, "ticket_detail.html", context, status_code=502)
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
    rag_index: RAGIndex = Depends(get_rag_index), settings: Settings = Depends(get_settings),
):
    if crud.get_ticket(db, ticket_id) is None:
        raise HTTPException(status_code=404, detail=f"Ticket {ticket_id} not found")
    try:
        crud.create_draft(db, ticket_id, recipient_email, subject, body, created_by_user_id=user.id)
    except ValidationError as e:
        return _render_ticket_error(request, db, ticket_id, rag_index, user, settings, f"Draft not saved - {e.message}.")
    return RedirectResponse(url=f"/ui/tickets/{ticket_id}", status_code=303)


@app.post("/ui/tickets/{ticket_id}/messages/{message_id}", dependencies=[Depends(require_csrf)])
def ui_update_draft_message(
    request: Request, ticket_id: int, message_id: int,
    recipient_email: str = Form(...), subject: str = Form(...), body: str = Form(...), version: int = Form(...),
    db: Session = Depends(get_db), user: User = Depends(require_role(UserRole.admin, UserRole.agent)),
    rag_index: RAGIndex = Depends(get_rag_index), settings: Settings = Depends(get_settings),
):
    # Confirms the message genuinely belongs to the ticket named in the
    # URL BEFORE any write - an external review found this wasn't
    # checked at all: crud.update_draft looked up the message by
    # message_id alone, so a valid agent hitting the wrong ticket_id in
    # the URL (typo, stale tab, guessed ID) could silently edit a
    # message under a completely different ticket. Checked here, not
    # inside crud.update_draft, so it fails BEFORE any side effect -
    # matching the review's own "return 404 before side effects on a
    # mismatch."
    existing = crud.get_message(db, message_id)
    if existing is None or existing.ticket_id != ticket_id:
        raise HTTPException(status_code=404, detail=f"Message {message_id} not found on ticket {ticket_id}")
    try:
        message = crud.update_draft(db, message_id, recipient_email, subject, body, expected_version=version)
    except ValidationError as e:
        return _render_ticket_error(request, db, ticket_id, rag_index, user, settings, f"Draft not saved - {e.message}.")
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
    # Same parent-relationship check as ui_update_draft_message, and
    # for the same reason - approving a message queues a real send, so
    # this check matters even more here than on an edit.
    existing = crud.get_message(db, message_id)
    if existing is None or existing.ticket_id != ticket_id:
        raise HTTPException(status_code=404, detail=f"Message {message_id} not found on ticket {ticket_id}")
    try:
        message = crud.approve_message(db, message_id, actor_user_id=user.id, expected_version=version, max_attempts=settings.outbox_max_attempts)
    except crud.VersionConflict:
        return _render_conflict(request, db, ticket_id, rag_index, user, settings)
    except crud.MessageNotDraft:
        return _render_conflict(request, db, ticket_id, rag_index, user, settings)
    if message is None:
        raise HTTPException(status_code=404, detail=f"Message {message_id} not found")
    return RedirectResponse(url=f"/ui/tickets/{ticket_id}", status_code=303)


@app.get("/reports")
def ui_reports(request: Request, db: Session = Depends(get_db), user: User = Depends(require_agent)):
    """Real queries over data already being tracked - ticket counts by
    status/priority/category, average resolution time (from the audit
    trail), and agent workload. Read-only, so any logged-in role
    (including reviewer) can view it."""
    data = crud.get_reports_data(db)

    def _as_bars(counts: dict) -> list[dict]:
        """Turns {"open": 12, "resolved": 4} into a list with a
        pre-computed bar-fill percentage, so the template only ever
        renders a number - no math in Jinja."""
        top = max(counts.values()) if counts else 0
        return [
            {"label": label, "count": count, "pct": round(100 * count / top) if top else 0}
            for label, count in counts.items()
        ]

    def _format_resolution_time(hours: Optional[float]) -> str:
        if hours is None:
            return "No resolved tickets yet"
        if hours < 1:
            return f"{round(hours * 60)} minutes"
        if hours < 48:
            return f"{hours:.1f} hours"
        return f"{hours / 24:.1f} days"

    return templates.TemplateResponse(
        request, "reports.html",
        {
            "user": user,
            "total": data["total"],
            "average_resolution_display": _format_resolution_time(data["average_resolution_hours"]),
            "agent_workload": data["agent_workload"],
            "status_bars": _as_bars(data["by_status"]),
            "priority_bars": _as_bars(data["by_priority"]),
            "category_bars": _as_bars(data["by_category"]),
        },
    )


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


@app.get("/kb")
def ui_kb_list(request: Request, q: Optional[str] = None, db: Session = Depends(get_db), user: User = Depends(require_agent)):
    articles = crud.list_kb_articles(db, q=q)
    grouped: dict[str, list] = {}
    for article in articles:
        grouped.setdefault(article.category, []).append(article)
    return templates.TemplateResponse(
        request, "kb_list.html", {"grouped_articles": grouped, "total_count": len(articles), "current_q": q, "user": user},
    )


@app.get("/kb/new")
def ui_kb_new_form(request: Request, user: User = Depends(require_role(UserRole.admin, UserRole.agent)), settings: Settings = Depends(get_settings)):
    return templates.TemplateResponse(
        request, "kb_edit.html",
        {"article": None, "user": user, "csrf_token": csrf_token_for_template(request, settings), "error": None},
    )


@app.post("/kb", dependencies=[Depends(require_role(UserRole.admin, UserRole.agent)), Depends(require_csrf)])
def ui_kb_create(
    request: Request, title: str = Form(...), category: str = Form(...), content: str = Form(...),
    db: Session = Depends(get_db),
    user: User = Depends(require_role(UserRole.admin, UserRole.agent)),
    settings: Settings = Depends(get_settings),
):
    """
    Creates the article, then IMMEDIATELY rebuilds the cached RAGIndex
    on app.state so the very next /suggest request sees it - the same
    cache that's normally only built once at startup (for performance -
    see supportrag.py) now also gets rebuilt on this deliberately rare,
    admin-only write path, which doesn't reintroduce the per-request
    refit cost this project already fixed once.

    Calls seed_knowledge_base FIRST, before the new row is inserted -
    found via testing, not assumed: build_rag_index's own seeding-if-
    empty check would otherwise see this new row and conclude the
    table "isn't empty", silently skipping the initial 17-article seed
    on a database where this happened to be the very first write. A
    real (if narrow) production edge case - an admin's first action
    being a KB article before any ticket ever ran /suggest - not just a
    test artifact.
    """
    seed_knowledge_base(db)
    form = {"title": title, "category": category, "content": content}
    try:
        article = _apply_kb_change(request, db, lambda d: crud.create_kb_article(d, title, category, content, commit=False))
    except (ValidationError, KBChangeRejected) as exc:
        db.rollback()
        return _kb_form_error(request, user, settings, None, form, f"Article not saved - {getattr(exc, 'message', exc)}")
    return RedirectResponse(url=f"/kb/{article.id}", status_code=303)


@app.get("/kb/{article_id}")
def ui_kb_detail(request: Request, article_id: int, db: Session = Depends(get_db), user: User = Depends(require_agent), settings: Settings = Depends(get_settings)):
    article = crud.get_kb_article(db, article_id)
    if article is None:
        raise HTTPException(status_code=404, detail=f"Article {article_id} not found")
    return templates.TemplateResponse(
        request, "kb_edit.html",
        {"article": article, "user": user, "csrf_token": csrf_token_for_template(request, settings), "error": None},
    )


@app.post("/kb/{article_id}", dependencies=[Depends(require_role(UserRole.admin, UserRole.agent)), Depends(require_csrf)])
def ui_kb_update(
    request: Request, article_id: int, title: str = Form(...), category: str = Form(...), content: str = Form(...),
    db: Session = Depends(get_db),
    user: User = Depends(require_role(UserRole.admin, UserRole.agent)),
    settings: Settings = Depends(get_settings),
):
    existing = crud.get_kb_article(db, article_id)
    if existing is None:
        raise HTTPException(status_code=404, detail=f"Article {article_id} not found")
    form = {"title": title, "category": category, "content": content}
    try:
        _apply_kb_change(request, db, lambda d: crud.update_kb_article(d, article_id, title, category, content, commit=False))
    except (ValidationError, KBChangeRejected) as exc:
        db.rollback()
        return _kb_form_error(request, user, settings, crud.get_kb_article(db, article_id), form,
                              f"Changes not saved - {getattr(exc, 'message', exc)}")
    return RedirectResponse(url=f"/kb/{article_id}", status_code=303)


@app.post("/kb/{article_id}/delete", dependencies=[Depends(require_role(UserRole.admin, UserRole.agent)), Depends(require_csrf)])
def ui_kb_delete(request: Request, article_id: int, confirm_delete: str = Form(default=""), db: Session = Depends(get_db), user: User = Depends(require_role(UserRole.admin, UserRole.agent)), settings: Settings = Depends(get_settings)):
    """
    Blocks deleting below 2 remaining articles - not an arbitrary
    limit, a real technical one: the TF-IDF/SVD embedder needs at
    least 2 documents to fit any meaningful space at all (see
    supportrag.build_rag_index's own guard). Below that, SupportRAG
    would silently abstain on everything rather than erroring, which
    is a worse failure mode than just refusing the delete with a clear
    reason.
    """
    if confirm_delete != "yes":
        # Server-side half of B8's fix: the browser checkbox is a
        # convenience; this is the actual guard against accidental deletes.
        raise HTTPException(status_code=400, detail="Deletion not confirmed. Tick the confirmation box and try again.")
    if crud.count_kb_articles(db) <= 2:
        article = crud.get_kb_article(db, article_id)
        return templates.TemplateResponse(
            request, "kb_edit.html",
            {
                "article": article, "user": user, "csrf_token": csrf_token_for_template(request, settings),
                "error": "Can't delete this - at least 2 knowledge base articles must remain for SupportRAG to work at all.",
            },
            status_code=409,
        )
    try:
        _apply_kb_change(request, db, lambda d: crud.delete_kb_article(d, article_id, commit=False))
    except KBChangeRejected as exc:
        return _kb_form_error(request, user, settings, crud.get_kb_article(db, article_id), None,
                              f"Not deleted - the remaining articles couldn't be indexed: {exc}", status_code=409)
    return RedirectResponse(url="/kb", status_code=303)


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
