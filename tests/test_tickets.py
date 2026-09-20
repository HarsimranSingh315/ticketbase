"""
Automated tests for TicketBase.

Run with: pytest -v

Shared fixtures (engine, session override, `client`) live in
conftest.py - see the docstring there for why they were pulled out of
this file (a cross-file test-isolation bug, fixed by having a single
shared override instead of one per file).
"""
import pytest


def test_health_check(client):
    resp = client.get("/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert body["database"] == "ok"


def test_create_ticket_returns_201_and_ticket_data(client):
    resp = client.post("/tickets", json={"description": "My mouse stopped working"})
    assert resp.status_code == 201
    data = resp.json()
    assert data["description"] == "My mouse stopped working"
    assert data["status"] == "open"
    assert data["category"] is None
    assert data["category_confirmed"] is False


@pytest.mark.parametrize("description,expected_priority", [
    ("The server is down", "high"),
    ("I cannot access my account", "high"),
    ("How do I change my password", "low"),
    ("Feature request: dark mode", "low"),
    ("Charged twice", "high"),
    ("My monitor flickers sometimes", "medium"),
])
def test_priority_is_computed_correctly(client, description, expected_priority):
    resp = client.post("/tickets", json={"description": description})
    assert resp.json()["priority"] == expected_priority


def test_get_nonexistent_ticket_returns_404(client):
    resp = client.get("/tickets/9999")
    assert resp.status_code == 404


def test_list_tickets_filters_by_status(client):
    client.post("/tickets", json={"description": "ticket one"})
    ticket_two = client.post("/tickets", json={"description": "ticket two"}).json()
    client.patch(f"/tickets/{ticket_two['id']}/status", json={"status": "resolved"})
    open_tickets = client.get("/tickets", params={"status": "open"}).json()
    resolved_tickets = client.get("/tickets", params={"status": "resolved"}).json()
    assert len(open_tickets) == 1
    assert len(resolved_tickets) == 1
    assert resolved_tickets[0]["id"] == ticket_two["id"]


def test_category_confirmation_is_a_separate_explicit_step(client):
    ticket = client.post("/tickets", json={"description": "WiFi keeps dropping"}).json()
    assert ticket["category"] is None
    assert ticket["category_confirmed"] is False
    resp = client.patch(f"/tickets/{ticket['id']}/category", json={"category": "connectivity"})
    updated = resp.json()
    assert updated["category"] == "connectivity"
    assert updated["category_confirmed"] is True


def test_update_status_on_nonexistent_ticket_returns_404(client):
    resp = client.patch("/tickets/9999/status", json={"status": "resolved"})
    assert resp.status_code == 404


def test_invalid_status_value_is_rejected(client):
    ticket = client.post("/tickets", json={"description": "test"}).json()
    resp = client.patch(f"/tickets/{ticket['id']}/status", json={"status": "not_a_real_status"})
    assert resp.status_code == 422
