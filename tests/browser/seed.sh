B=http://127.0.0.1:8811
curl -s -X POST $B/tickets -H 'content-type: application/json' -d '{"description":"My VPN will not connect after the Windows update this morning"}' > /dev/null
curl -s -X POST $B/tickets -H 'content-type: application/json' -d '{"description":"Printer on floor 3 shows offline for everyone"}' > /dev/null
cd /home/claude/ticketbase && . /tmp/venv/bin/activate && DATABASE_URL=sqlite:///./browser.db PYTHONPATH=. python - << 'PY' 2>/dev/null
from app.database import SessionLocal
from app import crud
db = SessionLocal()
c = crud.create_customer(db, "Northwind Traders")
crud.create_contact(db, customer_id=c.id, name="Dana Whitfield", email="dana@northwind.test", phone="+14035550199")
t = crud.get_ticket(db, 1); t.customer_id = c.id; db.commit()
crud.create_note(db, 1, 1, "Customer mentioned this started after last night's patch.")
crud.create_draft(db, 1, "dana@northwind.test", "Re: VPN connection", "Hi Dana, could you try restarting the VPN client?", created_by_user_id=1)
PY
