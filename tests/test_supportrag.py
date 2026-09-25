"""
Tests for SupportRAG (Project 2): the suggest-only category/draft
endpoint. These deliberately check the "never auto-applies" guarantee
as well as the happy path and the abstain path, since that guarantee is
the whole point of the design.

Shared fixtures (engine, session override, `client`) live in
conftest.py.
"""
from tests.conftest import csrf_headers


def test_suggest_returns_relevant_category_with_sources(admin_client):
    ticket = admin_client.post("/tickets", json={"description": "My VPN will not connect to the office"}).json()
    resp = admin_client.post(f"/tickets/{ticket['id']}/suggest", headers=csrf_headers(admin_client))
    assert resp.status_code == 200
    data = resp.json()
    assert data["abstained"] is False
    assert data["category"] == "connectivity"
    assert data["confidence"] > 0
    assert len(data["sources"]) > 0
    assert "draft_response" in data and len(data["draft_response"]) > 0


def test_suggest_never_writes_to_the_ticket(admin_client):
    ticket = admin_client.post("/tickets", json={"description": "Charged twice for my subscription"}).json()
    admin_client.post(f"/tickets/{ticket['id']}/suggest", headers=csrf_headers(admin_client))
    # Re-fetch the ticket: category must still be unset, since suggest
    # is read-only and only confirm_category is allowed to write it.
    refetched = admin_client.get(f"/tickets/{ticket['id']}").json()
    assert refetched["category"] is None
    assert refetched["category_confirmed"] is False


def test_suggest_on_nonexistent_ticket_returns_404(admin_client):
    resp = admin_client.post("/tickets/9999/suggest", headers=csrf_headers(admin_client))
    assert resp.status_code == 404


def test_suggest_then_confirm_accepts_the_suggested_category(admin_client):
    ticket = admin_client.post("/tickets", json={"description": "Suspicious login on my account, is this a breach?"}).json()
    suggestion = admin_client.post(f"/tickets/{ticket['id']}/suggest", headers=csrf_headers(admin_client)).json()
    assert suggestion["abstained"] is False

    confirm_resp = admin_client.patch(
        f"/tickets/{ticket['id']}/category", json={"category": suggestion["category"], "version": ticket["version"]}
    )
    assert confirm_resp.status_code == 200
    updated = confirm_resp.json()
    assert updated["category"] == suggestion["category"]
    assert updated["category_confirmed"] is True


def test_suggest_abstains_on_unrelated_gibberish(admin_client):
    ticket = admin_client.post("/tickets", json={"description": "xk qz zzz flumox glorbnax"}).json()
    resp = admin_client.post(f"/tickets/{ticket['id']}/suggest", headers=csrf_headers(admin_client))
    data = resp.json()
    assert data["abstained"] is True
    assert data["category"] is None


def test_seed_only_runs_on_an_empty_table_never_overwrites_admin_content(client, db_session):
    """
    Regression test for a real architectural conflict, found and fixed
    while building KB management UI: an earlier version of
    seed_knowledge_base actively reseeded the table whenever its
    content didn't match KB_ARTICLES - reasonable when KB_ARTICLES was
    the only way to add an article, but WRONG once admins can add real
    articles through the UI, since the next app startup would have
    silently deleted their work. This confirms the fix: a table that
    already has ANY content (even content that doesn't match
    KB_ARTICLES at all) is left completely alone.
    """
    from app.models import KnowledgeArticle
    from app.supportrag import seed_knowledge_base

    db = db_session
    db.query(KnowledgeArticle).delete()
    db.commit()

    admin_article = KnowledgeArticle(
        title="A totally custom admin-added article",
        category="custom",
        content="Content an admin wrote themselves, matching nothing in KB_ARTICLES.",
    )
    admin_article.embedding = "[]"
    db.add(admin_article)
    db.commit()

    seed_knowledge_base(db)

    remaining = db.query(KnowledgeArticle).all()
    assert len(remaining) == 1
    assert remaining[0].title == "A totally custom admin-added article"


def test_build_rag_index_picks_up_content_only_edits(client, db_session):
    """
    A real, previously-documented gap this refactor fixes as a side
    effect: the old title-based drift detection could miss a
    content-only edit (same title, changed body), since it only
    compared title SETS. build_rag_index now recomputes every
    embedding from current DB content on every call, making that class
    of staleness structurally impossible rather than just less likely.

    Uses 5 articles, not 2 - with too few documents, TruncatedSVD's
    component count degenerates to just 1 dimension (min(N_COMPONENTS,
    docs-1) - found by actually running this test with 2 articles
    first: it failed, because a single-dimension embedding is too
    lossy to reliably distinguish even very different content. That's
    a real, if narrow, property of a tiny corpus - not a bug in this
    feature - and the fix here is a more realistic test corpus, not a
    weaker assertion.
    """
    from app.embeddings import cosine_similarity
    from app.models import KnowledgeArticle
    from app.supportrag import build_rag_index

    db = db_session
    db.query(KnowledgeArticle).delete()
    db.commit()

    filler_topics = [
        ("Printer issue", "Printers jamming and running low on toner."),
        ("Wifi issue", "Wifi dropping out intermittently on laptops."),
        ("Login issue", "Users locked out after too many failed password attempts."),
        ("Billing issue", "Customers charged twice for the same monthly invoice."),
    ]
    target = KnowledgeArticle(title="Target article", category="hardware", content="Original content about batteries draining fast on a laptop.")
    target.embedding = "[]"
    db.add(target)
    for title, content in filler_topics:
        row = KnowledgeArticle(title=title, category="misc", content=content)
        row.embedding = "[]"
        db.add(row)
    db.commit()

    index_before = build_rag_index(db)
    before_vec = next(a.embedding for a in index_before.articles if a.id == target.id)

    # Edit the content directly (same title, same row) - simulates
    # what the /kb edit route does before calling build_rag_index again.
    target.content = "Completely different topic now: step-by-step VPN connectivity troubleshooting instructions for remote workers."
    db.commit()

    index_after = build_rag_index(db)
    after_vec = next(a.embedding for a in index_after.articles if a.id == target.id)

    # The embedding must have actually changed to reflect the new
    # content - not stayed stale from the pre-edit fit.
    assert cosine_similarity(before_vec, after_vec) < 0.99
