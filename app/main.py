"""
TicketBase API + minimal web UI.

Run it with:
    uvicorn app.main:app --reload

Then visit http://127.0.0.1:8000/docs for FastAPI's auto-generated,
interactive API documentation.
Visit http://127.0.0.1:8000/ for the web UI.

Production-readiness features (see README for the full writeup):
- Config via app/config.py (.env-driven), not hardcoded values
- The SupportRAG embedder is built ONCE at startup (RAGIndex, cached on
  app.state), not refit on every request
- Optional API-key auth on write endpoints (off by default; see app/auth.py)
- Rate limiting on the compute-heavier /suggest endpoint
- Structured logging with per-request timing
- Consistent JSON error responses (no leaked stack traces)
- Pagination on GET /tickets
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
from app.auth import require_api_key
from app.related_tickets import find_related_tickets
from app.supportrag import SupportRAGService, RAGIndex, build_rag_index

settings = get_settings()

logging.basicConfig(
    level=settings.log_level,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger("ticketbase")


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Dev-friendly auto-create. Production deployments should instead
    # run `alembic upgrade head` before starting the app (see
    # alembic/README / the migrations section in the project README) -
    # this create_all is a no-op against a DB that's already migrated.
    Base.metadata.create_all(bind=engine)

    db = SessionLocal()
    try:
        app.state.rag_index = build_rag_index(db)
    finally:
        db.close()

    logger.info("TicketBase startup complete (db=%s)", settings.database_url.split("://")[0])
    yield
    logger.info("TicketBase shutting down")


limiter = Limiter(key_func=get_remote_address)

app = FastAPI(title="TicketBase", version="0.2.0", lifespan=lifespan)
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)
app.mount("/static", StaticFiles(directory="app/static"), name="static")
templates = Jinja2Templates(directory="app/templates")


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


# --- Web UI routes ---
#
# These are deliberately separate from the JSON API routes above.
# They render HTML (Jinja2 templates) and handle browser form submissions,
# but they call the exact same crud.py functions the API and CLI use -
# no logic is duplicated, only the presentation differs. Not gated by
# require_api_key: that's for the JSON API (machine clients sending a
# header); a browser session is a different auth concern, out of scope
# here (see README's Non-goals).

@app.get("/")
def ui_index(request: Request, status: Optional[str] = None, q: Optional[str] = None, db: Session = Depends(get_db)):
    tickets = crud.list_tickets(db, status=status, q=q, limit=settings.max_page_size)
    stats = crud.get_ticket_stats(db)
    return templates.TemplateResponse(
        request, "index.html", {"tickets": tickets, "current_status": status, "current_q": q, "stats": stats}
    )


@app.post("/ui/tickets")
def ui_create_ticket(request: Request, description: str = Form(...), db: Session = Depends(get_db)):
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
):
    ticket = crud.get_ticket(db, ticket_id)
    if ticket is None:
        raise HTTPException(status_code=404, detail=f"Ticket {ticket_id} not found")
    candidates = crud.list_other_tickets(db, exclude_id=ticket_id)
    related = find_related_tickets(rag_index.embedder, ticket.description, candidates)
    return templates.TemplateResponse(
        request, "ticket_detail.html", {"ticket": ticket, "related_tickets": related}
    )


@app.post("/ui/tickets/{ticket_id}/suggest")
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
        request, "ticket_detail.html", {"ticket": ticket, "suggestion": suggestion, "related_tickets": related}
    )


@app.post("/ui/tickets/{ticket_id}/status")
def ui_update_status(ticket_id: int, status: str = Form(...), db: Session = Depends(get_db)):
    ticket = crud.update_status(db, ticket_id, status)
    if ticket is None:
        raise HTTPException(status_code=404, detail=f"Ticket {ticket_id} not found")
    return RedirectResponse(url=f"/ui/tickets/{ticket_id}", status_code=303)


@app.post("/ui/tickets/{ticket_id}/category")
def ui_confirm_category(ticket_id: int, category: str = Form(...), db: Session = Depends(get_db)):
    ticket = crud.confirm_category(db, ticket_id, category)
    if ticket is None:
        raise HTTPException(status_code=404, detail=f"Ticket {ticket_id} not found")
    return RedirectResponse(url=f"/ui/tickets/{ticket_id}", status_code=303)
