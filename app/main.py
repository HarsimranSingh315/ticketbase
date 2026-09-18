"""
TicketBase API.

Run it with:
    uvicorn app.main:app --reload

Then visit http://127.0.0.1:8000/docs for FastAPI's auto-generated,
interactive API documentation - this is a genuine feature, not a toy;
it's how you'll manually poke at the API while building the CLI.
"""
from fastapi import FastAPI, Depends, HTTPException
from sqlalchemy.orm import Session

from app.database import engine, Base, get_db
from app import crud, schemas

# Creates the tickets table if it doesn't exist yet. Fine for Week 1;
# once this is a "real" project, you'd use Alembic migrations instead
# of this auto-create (worth learning, but not a Week 1 concern).
Base.metadata.create_all(bind=engine)

app = FastAPI(title="TicketBase", version="0.1.0")


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
