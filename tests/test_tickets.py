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
    assert body["status"] == "ready"
    assert body["checks"]["database"] == "ok"


def test_liveness_needs_nothing_external(client):
    assert client.get("/live").json() == {"status": "alive"}


def test_readiness_returns_503_when_database_unreachable(client):
    """The old /health returned HTTP 200 with a 'degraded' body when the
    DB was down; platform health checks only read the status code."""
    from app.main import app
    from app.database import get_db

    class BrokenSession:
        def execute(self, *a, **k): raise RuntimeError("db down")
        def rollback(self): pass

    app.dependency_overrides[get_db] = lambda: BrokenSession()
    try:
        r = client.get("/ready")
    finally:
        from tests.conftest import override_get_db
        app.dependency_overrides[get_db] = override_get_db
    assert r.status_code == 503
    assert r.json()["checks"]["database"] == "unreachable"


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


def test_get_nonexistent_ticket_returns_404(admin_client):
    resp = admin_client.get("/tickets/9999")
    assert resp.status_code == 404


def test_list_tickets_filters_by_status(admin_client):
    admin_client.post("/tickets", json={"description": "ticket one"})
    ticket_two = admin_client.post("/tickets", json={"description": "ticket two"}).json()
    admin_client.patch(f"/tickets/{ticket_two['id']}/status", json={"status": "resolved", "version": 1})
    open_tickets = admin_client.get("/tickets", params={"status": "open"}).json()
    resolved_tickets = admin_client.get("/tickets", params={"status": "resolved"}).json()
    assert len(open_tickets) == 1
    assert len(resolved_tickets) == 1
    assert resolved_tickets[0]["id"] == ticket_two["id"]


def test_category_confirmation_is_a_separate_explicit_step(client):
    ticket = client.post("/tickets", json={"description": "WiFi keeps dropping"}).json()
    assert ticket["category"] is None
    assert ticket["category_confirmed"] is False
    resp = client.patch(f"/tickets/{ticket['id']}/category", json={"category": "connectivity", "version": 1})
    updated = resp.json()
    assert updated["category"] == "connectivity"
    assert updated["category_confirmed"] is True


def test_update_status_on_nonexistent_ticket_returns_404(client):
    resp = client.patch("/tickets/9999/status", json={"status": "resolved", "version": 1})
    assert resp.status_code == 404


def test_invalid_status_value_is_rejected(client):
    ticket = client.post("/tickets", json={"description": "test"}).json()
    resp = client.patch(f"/tickets/{ticket['id']}/status", json={"status": "not_a_real_status", "version": 1})
    assert resp.status_code == 422


def test_list_tickets_search_by_description(admin_client):
    admin_client.post("/tickets", json={"description": "My VPN will not connect"})
    admin_client.post("/tickets", json={"description": "Charged twice for my order"})
    admin_client.post("/tickets", json={"description": "Printer is offline"})

    results = admin_client.get("/tickets", params={"q": "VPN"}).json()
    assert len(results) == 1
    assert "VPN" in results[0]["description"]

    # Search is case-insensitive
    results_lower = admin_client.get("/tickets", params={"q": "vpn"}).json()
    assert len(results_lower) == 1

    no_match = admin_client.get("/tickets", params={"q": "nonexistent-keyword-xyz"}).json()
    assert no_match == []


def test_list_tickets_search_combines_with_status_filter(admin_client):
    t1 = admin_client.post("/tickets", json={"description": "VPN connection issue"}).json()
    t2 = admin_client.post("/tickets", json={"description": "VPN certificate expired"}).json()
    admin_client.patch(f"/tickets/{t2['id']}/status", json={"status": "resolved", "version": 1})

    open_vpn = admin_client.get("/tickets", params={"q": "VPN", "status": "open"}).json()
    assert len(open_vpn) == 1
    assert open_vpn[0]["id"] == t1["id"]


def test_b1_invalid_ui_status_is_rejected_and_never_stored(admin_client, db_session):
    """B1 reproduction: status=BOGUS used to be committed, after which
    every read of the ticket raised LookupError (a bricked ticket)."""
    from tests.conftest import get_csrf_token
    from app import crud
    t = admin_client.post("/tickets", json={"description": "b1"}).json()
    csrf = get_csrf_token(admin_client, f"/ui/tickets/{t['id']}")
    r = admin_client.post(f"/ui/tickets/{t['id']}/status",
                          data={"status": "BOGUS", "version": t["version"], "csrf_token": csrf}, follow_redirects=False)
    assert r.status_code == 422
    assert "must be one of" in r.text
    db_session.expire_all()
    stored = crud.get_ticket(db_session, t["id"])
    assert stored.status.value == "open" and stored.version == t["version"]
    assert admin_client.get(f"/ui/tickets/{t['id']}").status_code == 200  # still readable


def test_b1_crud_layer_rejects_invalid_status_even_without_the_route(db_session):
    import pytest
    from app import crud
    from app.validation import ValidationError
    t = crud.create_ticket(db_session, "direct")
    with pytest.raises(ValidationError):
        crud.update_status(db_session, t.id, "BOGUS", expected_version=t.version)
