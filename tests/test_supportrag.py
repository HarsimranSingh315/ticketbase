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


def test_kb_drift_triggers_automatic_reseed(client, db_session):
    """
    Regression test for a real issue: adding a new article to
    KB_ARTICLES used to silently do nothing for anyone with an existing
    database, since the old seed logic only ran on an EMPTY table. This
    simulates that exact scenario - a DB seeded with an older, smaller
    KB_ARTICLES set - and confirms build_rag_index (called at app
    startup) detects the drift and reseeds automatically.
    """
    from app.embeddings import Embedder
    from app.kb_articles import KB_ARTICLES
    from app.models import KnowledgeArticle
    from app.supportrag import build_rag_index

    db = db_session
    # Wipe whatever the shared fixture already seeded, and reseed with
    # an artificially OLDER, smaller KB (one article missing).
    db.query(KnowledgeArticle).delete()
    db.commit()
    older_articles = KB_ARTICLES[:-1]
    e = Embedder()
    e.fit([a["content"] for a in older_articles])
    for a in older_articles:
        row = KnowledgeArticle(title=a["title"], category=a["category"], content=a["content"])
        row.set_embedding(e.embed(a["content"]))
        db.add(row)
    db.commit()
    assert db.query(KnowledgeArticle).count() == len(older_articles)

    # This is what app startup does - it should detect the drift and
    # bring the DB back in sync with the full KB_ARTICLES list.
    build_rag_index(db)

    assert db.query(KnowledgeArticle).count() == len(KB_ARTICLES)
    titles = {row.title for row in db.query(KnowledgeArticle.title).all()}
    assert titles == {a["title"] for a in KB_ARTICLES}
