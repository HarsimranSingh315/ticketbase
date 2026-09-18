"""
TicketBase API + minimal web UI.

Run it with:
    uvicorn app.main:app --reload

Then visit http://127.0.0.1:8000/docs for FastAPI's auto-generated,
interactive API documentation - this is a genuine feature, not a toy;
it's how you'll manually poke at the API while building the CLI.

Visit http://127.0.0.1:8000/ for the web UI.
"""
from fastapi import FastAPI, Depends, HTTPException, Request, Form
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.database import engine, Base, get_db
from app import crud, schemas

# Creates the tickets table if it doesn't exist yet. Fine for Week 1;
# once this is a "real" project, you'd use Alembic migrations instead
# of this auto-create (worth learning, but not a Week 1 concern).
Base.metadata.create_all(bind=engine)

app = FastAPI(title="TicketBase", version="0.1.0")
app.mount("/static", StaticFiles(directory="app/static"), name="static")
templates = Jinja2Templates(directory="app/templates")


@app.post("/tickets", response_model=schemas.TicketOut, status_code=201)
def create_ticket(payload: schemas.TicketCreate, db: Session = Depends(get_db)):
    ticket = crud.create_ticket(db, payload.description)
    return ticket


@app.get("/tickets", response_model=list[schemas.TicketOut])
def list_tickets(status: str | None = None, priority: str | None = None, db: Session = Depends(get_db)):
    return crud.list_tickets(db, status=status, priority=priority)


@app.get("/tickets/{ticket_id}", response_model=schemas.TicketOut)
def get_ticket(ticket_id: int, db: Session = Depends(get_db)):
    ticket = crud.get_ticket(db, ticket_id)
    if ticket is None:
        raise HTTPException(status_code=404, detail=f"Ticket {ticket_id} not found")
    return ticket


@app.patch("/tickets/{ticket_id}/status", response_model=schemas.TicketOut)
def update_ticket_status(ticket_id: int, payload: schemas.TicketStatusUpdate, db: Session = Depends(get_db)):
    ticket = crud.update_status(db, ticket_id, payload.status)
    if ticket is None:
        raise HTTPException(status_code=404, detail=f"Ticket {ticket_id} not found")
    return ticket


@app.patch("/tickets/{ticket_id}/category", response_model=schemas.TicketOut)
def confirm_ticket_category(ticket_id: int, payload: schemas.TicketCategoryConfirm, db: Session = Depends(get_db)):
    """
    Confirms a ticket's category. In Week 1, a human types this in directly.
    In Week 4+, an AI-suggested category will pre-fill this - but note it
    still requires this exact same explicit confirmation call. The AI
    never gets a shortcut around this endpoint.
    """
    ticket = crud.confirm_category(db, ticket_id, payload.category)
    if ticket is None:
        raise HTTPException(status_code=404, detail=f"Ticket {ticket_id} not found")
    return ticket


@app.get("/health")
def health_check():
    return {"status": "ok"}


# --- Web UI routes ---
#
# These are deliberately separate from the JSON API routes above.
# They render HTML (Jinja2 templates) and handle browser form submissions,
# but they call the exact same crud.py functions the API and CLI use -
# no logic is duplicated, only the presentation differs.

@app.get("/")
def ui_index(request: Request, status: str | None = None, db: Session = Depends(get_db)):
    tickets = crud.list_tickets(db, status=status)
    return templates.TemplateResponse(
        request, "index.html", {"tickets": tickets, "current_status": status}
    )


@app.post("/ui/tickets")
def ui_create_ticket(description: str = Form(...), db: Session = Depends(get_db)):
    crud.create_ticket(db, description)
    return RedirectResponse(url="/", status_code=303)


@app.get("/ui/tickets/{ticket_id}")
def ui_ticket_detail(request: Request, ticket_id: int, db: Session = Depends(get_db)):
    ticket = crud.get_ticket(db, ticket_id)
    if ticket is None:
        raise HTTPException(status_code=404, detail=f"Ticket {ticket_id} not found")
    return templates.TemplateResponse(
        request, "ticket_detail.html", {"ticket": ticket}
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
