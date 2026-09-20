"""
Tests for SupportRAG (Project 2): the suggest-only category/draft
endpoint. These deliberately check the "never auto-applies" guarantee
as well as the happy path and the abstain path, since that guarantee is
the whole point of the design.

Shared fixtures (engine, session override, `client`) live in
conftest.py.
"""


def test_suggest_returns_relevant_category_with_sources(client):
    ticket = client.post("/tickets", json={"description": "My VPN will not connect to the office"}).json()
    resp = client.post(f"/tickets/{ticket['id']}/suggest")
    assert resp.status_code == 200
    data = resp.json()
    assert data["abstained"] is False
    assert data["category"] == "connectivity"
    assert data["confidence"] > 0
    assert len(data["sources"]) > 0
    assert "draft_response" in data and len(data["draft_response"]) > 0


def test_suggest_never_writes_to_the_ticket(client):
    ticket = client.post("/tickets", json={"description": "Charged twice for my subscription"}).json()
    client.post(f"/tickets/{ticket['id']}/suggest")
    # Re-fetch the ticket: category must still be unset, since suggest
    # is read-only and only confirm_category is allowed to write it.
    refetched = client.get(f"/tickets/{ticket['id']}").json()
    assert refetched["category"] is None
    assert refetched["category_confirmed"] is False


def test_suggest_on_nonexistent_ticket_returns_404(client):
    resp = client.post("/tickets/9999/suggest")
    assert resp.status_code == 404


def test_suggest_then_confirm_accepts_the_suggested_category(client):
    ticket = client.post("/tickets", json={"description": "Suspicious login on my account, is this a breach?"}).json()
    suggestion = client.post(f"/tickets/{ticket['id']}/suggest").json()
    assert suggestion["abstained"] is False

    confirm_resp = client.patch(
        f"/tickets/{ticket['id']}/category", json={"category": suggestion["category"]}
    )
    assert confirm_resp.status_code == 200
    updated = confirm_resp.json()
    assert updated["category"] == suggestion["category"]
    assert updated["category_confirmed"] is True


def test_suggest_abstains_on_unrelated_gibberish(client):
    ticket = client.post("/tickets", json={"description": "xk qz zzz flumox glorbnax"}).json()
    resp = client.post(f"/tickets/{ticket['id']}/suggest")
    data = resp.json()
    assert data["abstained"] is True
    assert data["category"] is None
