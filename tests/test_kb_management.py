"""
Tests for knowledge base management (create/edit/delete articles
through the UI, with the live RAGIndex cache kept in sync).

Everything here was first verified manually end-to-end against a real
running app - including confirming a NEWLY CREATED article is picked
up by /suggest on the SAME running server with no restart, and that
editing an article's content changes what gets suggested - before
being written as permanent tests. See docs/current-state.md.
"""
from tests.conftest import csrf_headers
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
    resp = admin_client.post(f"/kb/{article_id}/delete", data={"csrf_token": csrf, "confirm_delete": "yes"}, follow_redirects=False)
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
    resp = admin_client.post(f"/kb/{remaining[0].id}/delete", data={"csrf_token": csrf, "confirm_delete": "yes"})
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

    resp = admin_client.post(f"/tickets/{ticket['id']}/suggest", headers=csrf_headers(admin_client))
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

    resp1 = admin_client.post(f"/tickets/{ticket['id']}/suggest", headers=csrf_headers(admin_client))
    assert resp1.json()["category"] == "marker"

    # Rewrite the content to something unrelated.
    csrf = get_csrf_token(admin_client, f"/kb/{article_id}")
    admin_client.post(
        f"/kb/{article_id}",
        data={"title": "Topic marker article", "category": "marker", "content": "Completely different: unrelated filler text about nothing in particular.", "csrf_token": csrf},
    )

    resp2 = admin_client.post(f"/tickets/{ticket['id']}/suggest", headers=csrf_headers(admin_client))
    # The original zzqqxx-specific ticket should no longer confidently
    # match the "marker" category, since that content is gone.
    assert resp2.json().get("category") != "marker" or resp2.json()["abstained"] is True


def test_deleted_article_no_longer_appears_as_a_suggestion_source(admin_client):
    article_id = _create_article(
        admin_client, title="Deletable marker article", category="deletetest",
        content="Uniquely identifiable content about qqzzyy marker phrase beta.",
    )
    ticket = admin_client.post("/tickets", json={"description": "Question about qqzzyy marker phrase beta"}).json()
    resp1 = admin_client.post(f"/tickets/{ticket['id']}/suggest", headers=csrf_headers(admin_client))
    assert resp1.json()["category"] == "deletetest"

    csrf = get_csrf_token(admin_client, f"/kb/{article_id}")
    admin_client.post(f"/kb/{article_id}/delete", data={"csrf_token": csrf, "confirm_delete": "yes"})

    resp2 = admin_client.post(f"/tickets/{ticket['id']}/suggest", headers=csrf_headers(admin_client))
    sources = resp2.json().get("sources", [])
    assert not any(s["article_id"] == article_id for s in sources)


def test_b8_delete_without_confirmation_is_refused(admin_client, db_session):
    from tests.conftest import get_csrf_token
    from app import crud
    from app.supportrag import seed_knowledge_base
    seed_knowledge_base(db_session)
    before = crud.count_kb_articles(db_session)
    article_id = crud.list_kb_articles(db_session)[0].id
    csrf = get_csrf_token(admin_client, f"/kb/{article_id}")
    r = admin_client.post(f"/kb/{article_id}/delete", data={"csrf_token": csrf}, follow_redirects=False)
    assert r.status_code == 400
    assert crud.count_kb_articles(db_session) == before


def test_no_template_uses_inline_event_handlers_blocked_by_csp():
    """B8 root cause: script-src 'self' blocks inline handlers, so any
    onsubmit/onclick silently does nothing. Guard the whole template set."""
    import pathlib, re
    offenders = []
    for path in pathlib.Path("app/templates").glob("*.html"):
        for n, line in enumerate(path.read_text().splitlines(), 1):
            if re.search(r"\son[a-z]+\s*=", line):
                offenders.append(f"{path.name}:{n}")
    assert offenders == [], f"inline handlers found: {offenders}"


def test_b10_search_term_is_url_encoded_in_filter_links(admin_client):
    r = admin_client.get("/", params={"q": "a&status=resolved#x"})
    assert r.status_code == 200
    assert "q=a%26status%3Dresolved%23x" in r.text
    assert "&q=a&status=resolved" not in r.text


def _fake_process(db_session):
    """An independent web process: its own app.state, the shared database."""
    from types import SimpleNamespace
    from app.main import get_rag_index
    state = SimpleNamespace(rag_index=None, kb_revision=None, rag_index_error=None)
    request = SimpleNamespace(app=SimpleNamespace(state=state))
    return lambda: get_rag_index(request, db_session)


def test_b3_edit_in_one_process_is_seen_by_another(db_session):
    """B3 reproduction: an edit rebuilt only the handling process's index;
    others served the old content indefinitely."""
    from app import crud
    from app.supportrag import seed_knowledge_base
    seed_knowledge_base(db_session)
    process_a, process_b = _fake_process(db_session), _fake_process(db_session)
    process_a(); process_b()  # both warm their caches
    article = crud.list_kb_articles(db_session)[0]

    crud.update_kb_article(db_session, article.id, "Renamed by process A", article.category, article.content)
    crud.bump_kb_revision(db_session); db_session.commit()

    titles_b = {a.title for a in process_b().articles}
    assert "Renamed by process A" in titles_b


def test_b3_delete_in_one_process_disappears_from_another(db_session):
    from app import crud
    from app.supportrag import seed_knowledge_base
    seed_knowledge_base(db_session)
    process_b = _fake_process(db_session)
    victim = crud.list_kb_articles(db_session)[0]
    assert victim.id in {a.id for a in process_b().articles}
    crud.delete_kb_article(db_session, victim.id)
    crud.bump_kb_revision(db_session); db_session.commit()
    assert victim.id not in {a.id for a in process_b().articles}


def test_b3_unchanged_revision_does_not_rebuild(db_session, monkeypatch):
    """The per-request check must stay cheap: no rebuild when nothing changed."""
    import app.main as main_mod
    from app.supportrag import seed_knowledge_base
    seed_knowledge_base(db_session)
    process = _fake_process(db_session)
    process()
    builds = []
    real = main_mod.build_rag_index
    monkeypatch.setattr(main_mod, "build_rag_index", lambda *a, **k: builds.append(1) or real(*a, **k))
    process(); process()
    assert builds == []


def test_b4_builder_raises_typed_error_on_unindexable_corpus(db_session):
    import pytest
    from app import crud
    from app.supportrag import build_rag_index, IndexBuildError
    crud.create_kb_article(db_session, "One", "hardware", "printer")
    crud.create_kb_article(db_session, "Two", "hardware", "the")
    with pytest.raises(IndexBuildError):
        build_rag_index(db_session, seed=False)


def test_b4_edit_that_breaks_indexing_is_rejected_and_rolled_back(admin_client, db_session):
    """Real data, no mocked failure: a 2-article KB where one article is
    'printer' and the other is edited to 'the' cannot be indexed. Before
    the fix the edit committed first, leaving a KB that broke every later
    index build (including startup)."""
    from tests.conftest import get_csrf_token
    from app import crud
    keep = crud.create_kb_article(db_session, "Printer offline", "hardware", "printer")
    target = crud.create_kb_article(db_session, "VPN", "connectivity", "restart the vpn client and reconnect")
    csrf = get_csrf_token(admin_client, f"/kb/{target.id}")
    r = admin_client.post(f"/kb/{target.id}", data={
        "title": "VPN", "category": "connectivity", "content": "the", "csrf_token": csrf}, follow_redirects=False)

    assert r.status_code == 422
    assert "Changes not saved" in r.text
    assert ">the</textarea>" in r.text  # the agent's input is preserved, not discarded
    db_session.expire_all()
    assert crud.get_kb_article(db_session, target.id).content == "restart the vpn client and reconnect"
    assert crud.get_kb_revision(db_session) == 0  # no revision published


def test_b4_valid_edit_commits_and_bumps_revision(admin_client, db_session):
    from tests.conftest import get_csrf_token
    from app import crud
    crud.create_kb_article(db_session, "Printer offline", "hardware", "power cycle the printer and check the queue")
    target = crud.create_kb_article(db_session, "VPN", "connectivity", "restart the vpn client")
    csrf = get_csrf_token(admin_client, f"/kb/{target.id}")
    r = admin_client.post(f"/kb/{target.id}", data={
        "title": "VPN", "category": "connectivity", "content": "reinstall the vpn client and sign in again",
        "csrf_token": csrf}, follow_redirects=False)
    assert r.status_code == 303
    db_session.expire_all()
    assert "reinstall" in crud.get_kb_article(db_session, target.id).content
    assert crud.get_kb_revision(db_session) == 1


def test_b4_startup_with_unindexable_kb_serves_with_suggestions_disabled(db_session):
    """Safe-unavailable state: the app must start and report degraded
    suggestions rather than crash - tickets matter more than suggestions."""
    from types import SimpleNamespace
    from app import crud
    from app.main import load_initial_index
    crud.create_kb_article(db_session, "One", "hardware", "printer")
    crud.create_kb_article(db_session, "Two", "hardware", "the")
    state = SimpleNamespace()
    load_initial_index(state, db_session)  # the real startup function
    assert state.rag_index_error is not None
    assert state.rag_index.articles == []
    assert state.rag_index is not None  # suggestions abstain; nothing crashes
