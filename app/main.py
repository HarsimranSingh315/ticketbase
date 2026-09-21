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
from fastapi.responses import RedirectResponse, JSONResponse
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
    ticket = crud.update_status(db, ticket_id, payload.status)
    if ticket is None:
        raise HTTPException(status_code=404, detail=f"Ticket {ticket_id} not found")
    return ticket


@app.patch("/tickets/{ticket_id}/category", response_model=schemas.TicketOut, dependencies=[Depends(require_api_key)])
def confirm_ticket_category(ticket_id: int, payload: schemas.TicketCategoryConfirm, db: Session = Depends(get_db)):
    """
    Confirms a ticket's category. A human types this in directly, or an
    AI-suggested category (from /suggest) pre-fills it - but this exact
    same explicit confirmation call is still required either way. The AI
    never gets a shortcut around this endpoint.
    """
    ticket = crud.confirm_category(db, ticket_id, payload.category)
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
        {"ticket": ticket, "related_tickets": related, "user": user, "csrf_token": csrf_token_for_template(request, settings)},
    )


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
    return templates.TemplateResponse(
        request, "ticket_detail.html",
        {
            "ticket": ticket, "suggestion": suggestion, "related_tickets": related,
            "user": get_current_user(request, db), "csrf_token": csrf_token_for_template(request, rag_settings),
        },
    )


@app.post("/ui/tickets/{ticket_id}/status", dependencies=[Depends(require_role(UserRole.admin, UserRole.agent)), Depends(require_csrf)])
def ui_update_status(ticket_id: int, status: str = Form(...), db: Session = Depends(get_db)):
    ticket = crud.update_status(db, ticket_id, status)
    if ticket is None:
        raise HTTPException(status_code=404, detail=f"Ticket {ticket_id} not found")
    return RedirectResponse(url=f"/ui/tickets/{ticket_id}", status_code=303)


@app.post("/ui/tickets/{ticket_id}/category", dependencies=[Depends(require_role(UserRole.admin, UserRole.agent)), Depends(require_csrf)])
def ui_confirm_category(ticket_id: int, category: str = Form(...), db: Session = Depends(get_db)):
    ticket = crud.confirm_category(db, ticket_id, category)
    if ticket is None:
        raise HTTPException(status_code=404, detail=f"Ticket {ticket_id} not found")
    return RedirectResponse(url=f"/ui/tickets/{ticket_id}", status_code=303)
