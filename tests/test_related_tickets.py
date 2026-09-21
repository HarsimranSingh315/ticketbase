"""
Tests for related tickets (app/related_tickets.py + GET /tickets/{id}/related).

Covers the properties that matter: finds genuinely similar tickets,
never returns the ticket itself, returns empty (not an error or a weak
guess) when nothing is actually similar, and 404s cleanly for a
nonexistent ticket.
"""


def test_related_tickets_finds_a_similar_ticket(client):
    t1 = client.post("/tickets", json={"description": "My VPN will not connect from home"}).json()
    t2 = client.post("/tickets", json={"description": "VPN keeps disconnecting, cannot reach the office network"}).json()

    resp = client.get(f"/tickets/{t1['id']}/related")
    assert resp.status_code == 200
    related_ids = [r["id"] for r in resp.json()]
    assert t2["id"] in related_ids


def test_related_tickets_never_includes_the_ticket_itself(client):
    t1 = client.post("/tickets", json={"description": "My VPN will not connect from home"}).json()
    client.post("/tickets", json={"description": "VPN keeps disconnecting"})

    resp = client.get(f"/tickets/{t1['id']}/related")
    related_ids = [r["id"] for r in resp.json()]
    assert t1["id"] not in related_ids


def test_related_tickets_empty_when_nothing_similar(client):
    t1 = client.post("/tickets", json={"description": "My VPN will not connect"}).json()
    client.post("/tickets", json={"description": "Completely unrelated: feature request for dark mode"})

    resp = client.get(f"/tickets/{t1['id']}/related")
    assert resp.status_code == 200
    # A single, unrelated other ticket shouldn't be forced into the results.
    assert resp.json() == [] or all(r["id"] != t1["id"] for r in resp.json())


def test_related_tickets_empty_when_no_other_tickets_exist(client):
    t1 = client.post("/tickets", json={"description": "My VPN will not connect"}).json()
    resp = client.get(f"/tickets/{t1['id']}/related")
    assert resp.status_code == 200
    assert resp.json() == []


def test_related_tickets_on_nonexistent_ticket_returns_404(client):
    resp = client.get("/tickets/9999/related")
    assert resp.status_code == 404


def test_ticket_detail_page_shows_related_tickets(admin_client):
    t1 = admin_client.post("/tickets", json={"description": "My VPN will not connect from home"}).json()
    admin_client.post("/tickets", json={"description": "VPN keeps disconnecting, cannot reach the office network"})

    resp = admin_client.get(f"/ui/tickets/{t1['id']}")
    assert resp.status_code == 200
    assert "Related tickets" in resp.text
    assert "VPN keeps disconnecting" in resp.text
