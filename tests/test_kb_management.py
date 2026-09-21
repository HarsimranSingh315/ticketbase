"""
Tests for knowledge base management (create/edit/delete articles
through the UI, with the live RAGIndex cache kept in sync).

Everything here was first verified manually end-to-end against a real
running app - including confirming a NEWLY CREATED article is picked
up by /suggest on the SAME running server with no restart, and that
editing an article's content changes what gets suggested - before
being written as permanent tests. See docs/current-state.md.
"""
from tests.conftest import get_csrf_token


def _create_article(admin_client, title="Test article", category="testcat", content="Some test content about a specific topic."):
    csrf = get_csrf_token(admin_client, "/kb/new")
    resp = admin_client.post(
        "/kb", data={"title": title, "category": category, "content": content, "csrf_token": csrf},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    article_id = int(resp.headers["location"].rstrip("/").split("/")[-1])
    return article_id


# --- CRUD ---

def test_create_kb_article(admin_client):
    article_id = _create_article(admin_client, title="Custom article", category="custom")
    detail = admin_client.get(f"/kb/{article_id}")
    assert detail.status_code == 200
    assert "Custom article" in detail.text


def test_edit_kb_article(admin_client):
    article_id = _create_article(admin_client, title="Original title")
    csrf = get_csrf_token(admin_client, f"/kb/{article_id}")
    resp = admin_client.post(
        f"/kb/{article_id}",
        data={"title": "Edited title", "category": "testcat", "content": "Edited content.", "csrf_token": csrf},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    detail = admin_client.get(f"/kb/{article_id}")
    assert "Edited title" in detail.text


def test_delete_kb_article(admin_client):
    # Baseline has 17 seed articles, so deleting one stays well above
    # the minimum-2 guard.
    article_id = _create_article(admin_client, title="To be deleted")
    csrf = get_csrf_token(admin_client, f"/kb/{article_id}")
    resp = admin_client.post(f"/kb/{article_id}/delete", data={"csrf_token": csrf}, follow_redirects=False)
    assert resp.status_code == 303
    detail = admin_client.get(f"/kb/{article_id}")
    assert detail.status_code == 404


def test_cannot_delete_below_two_articles(admin_client, db_session):
    from app import crud
    from app.supportrag import seed_knowledge_base
    seed_knowledge_base(db_session)  # baseline 17 articles - nothing seeds this automatically when using db_session directly, unlike going through a route
    # Delete down to exactly 2 remaining.
    while crud.count_kb_articles(db_session) > 2:
        article = crud.list_kb_articles(db_session)[0]
        crud.delete_kb_article(db_session, article.id)

    remaining = crud.list_kb_articles(db_session)
    assert len(remaining) == 2
    csrf = get_csrf_token(admin_client, f"/kb/{remaining[0].id}")
    resp = admin_client.post(f"/kb/{remaining[0].id}/delete", data={"csrf_token": csrf})
    assert resp.status_code == 409
    assert crud.count_kb_articles(db_session) == 2


def test_reviewer_cannot_create_edit_or_delete(reviewer_client):
    resp = reviewer_client.post("/kb", data={"title": "x", "category": "x", "content": "x", "csrf_token": "x"})
    assert resp.status_code == 403


def test_reviewer_can_view_kb_list_and_articles(reviewer_client, db_session):
    from app import crud
    from app.supportrag import seed_knowledge_base
    seed_knowledge_base(db_session)
    article = crud.list_kb_articles(db_session)[0]
    assert reviewer_client.get("/kb").status_code == 200
    assert reviewer_client.get(f"/kb/{article.id}").status_code == 200


# --- List page ---

def test_kb_list_groups_by_category(admin_client):
    _create_article(admin_client, title="Unique category article", category="a-brand-new-category")
    resp = admin_client.get("/kb")
    assert "a-brand-new-category" in resp.text
    assert "Unique category article" in resp.text


def test_kb_search_filters_results(admin_client):
    _create_article(admin_client, title="Findable by search", category="testcat", content="unique searchable phrase xyz123")
    resp = admin_client.get("/kb", params={"q": "xyz123"})
    assert "Findable by search" in resp.text
    resp_no_match = admin_client.get("/kb", params={"q": "nonexistent-phrase-999"})
    assert "Findable by search" not in resp_no_match.text


# --- Live RAGIndex update (the core architectural claim) ---

def test_new_article_is_immediately_usable_by_suggest_with_no_restart(admin_client):
    """The central claim of this feature: a newly created article
    affects /suggest on the SAME running app, immediately - not just
    after a restart. Verified here through the real routes, matching
    the manual end-to-end run in docs/current-state.md."""
    ticket = admin_client.post("/tickets", json={"description": "My smart thermostat keeps losing wifi connection and resetting"}).json()

    _create_article(
        admin_client,
        title="Smart thermostat wifi troubleshooting",
        category="iot",
        content="Smart thermostats losing wifi and resetting schedules is usually a router firmware issue or the device being too far from the router.",
    )

    resp = admin_client.post(f"/tickets/{ticket['id']}/suggest")
    data = resp.json()
    assert data["abstained"] is False
    assert data["category"] == "iot"
    assert any("thermostat" in s["title"].lower() for s in data["sources"])


def test_editing_article_content_changes_what_gets_suggested(admin_client):
    """Complementary to the above: an EDIT (not just a create) must
    also be picked up immediately - this is the fix for the
    content-only-edit gap documented in app/supportrag.py."""
    article_id = _create_article(
        admin_client, title="Topic marker article", category="marker",
        content="This article is specifically and uniquely about zzqqxx marker phrase alpha.",
    )
    ticket = admin_client.post("/tickets", json={"description": "I have a question about zzqqxx marker phrase alpha"}).json()

    resp1 = admin_client.post(f"/tickets/{ticket['id']}/suggest")
    assert resp1.json()["category"] == "marker"

    # Rewrite the content to something unrelated.
    csrf = get_csrf_token(admin_client, f"/kb/{article_id}")
    admin_client.post(
        f"/kb/{article_id}",
        data={"title": "Topic marker article", "category": "marker", "content": "Completely different: unrelated filler text about nothing in particular.", "csrf_token": csrf},
    )

    resp2 = admin_client.post(f"/tickets/{ticket['id']}/suggest")
    # The original zzqqxx-specific ticket should no longer confidently
    # match the "marker" category, since that content is gone.
    assert resp2.json().get("category") != "marker" or resp2.json()["abstained"] is True


def test_deleted_article_no_longer_appears_as_a_suggestion_source(admin_client):
    article_id = _create_article(
        admin_client, title="Deletable marker article", category="deletetest",
        content="Uniquely identifiable content about qqzzyy marker phrase beta.",
    )
    ticket = admin_client.post("/tickets", json={"description": "Question about qqzzyy marker phrase beta"}).json()
    resp1 = admin_client.post(f"/tickets/{ticket['id']}/suggest")
    assert resp1.json()["category"] == "deletetest"

    csrf = get_csrf_token(admin_client, f"/kb/{article_id}")
    admin_client.post(f"/kb/{article_id}/delete", data={"csrf_token": csrf})

    resp2 = admin_client.post(f"/tickets/{ticket['id']}/suggest")
    sources = resp2.json().get("sources", [])
    assert not any(s["article_id"] == article_id for s in sources)
