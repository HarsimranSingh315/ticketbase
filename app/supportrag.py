"""
SupportRAG (Project 2): suggests a ticket category and drafts a response
by retrieving the most similar knowledge-base articles.

Hard rule carried over from Project 1's design: this module never writes
to a ticket's `category` / `category_confirmed` fields. It only returns a
Suggestion for a human to review. The only code path that can confirm a
category is still `crud.confirm_category()`, called from an explicit
human action (web UI button or CLI command).

Two failure modes this is designed to avoid:
1. A confident-sounding wrong answer with no way to check it -> every
   suggestion cites the specific KB articles it's based on, with their
   similarity scores, so a human can verify the reasoning, not just the
   conclusion.
2. Silently guessing on something outside the KB's coverage -> if the
   best match's similarity is below the confidence threshold, this
   abstains instead of forcing a low-quality suggestion.

Performance note (this used to be a real bug): an earlier version fit a
new TF-IDF/SVD embedder from scratch on EVERY request, inside
`SupportRAGService.__init__`. That's wasted CPU on every single call for
data that never changes between requests. The fix: `RAGIndex` is built
ONCE, at app startup (see `main.py`'s lifespan handler), cached on
`app.state`, and every request's `SupportRAGService` just reads from it -
no refitting, no per-request DB query for the article list.
"""
from __future__ import annotations

import logging
from collections import defaultdict
from dataclasses import dataclass, field

from sqlalchemy.orm import Session

from app.config import Settings
from app.embeddings import Embedder, cosine_similarity
from app.kb_articles import KB_ARTICLES
from app.llm import generate_grounded_draft
from app.models import KnowledgeArticle

logger = logging.getLogger("ticketbase.supportrag")


@dataclass
class Source:
    article_id: int
    title: str
    category: str
    similarity: float


@dataclass
class Suggestion:
    abstained: bool
    category: str | None
    confidence: float
    sources: list[Source] = field(default_factory=list)
    draft_response: str = ""
    draft_source: str = "template"  # "template" or "llm"


@dataclass
class _IndexedArticle:
    """
    A KB article's data pulled out of its SQLAlchemy row into a plain
    dataclass, so RAGIndex can be cached across requests/DB sessions
    without holding onto a detached ORM object.
    """
    id: int
    title: str
    category: str
    content: str
    embedding: list[float]


class RAGIndex:
    """
    The fitted embedder plus every KB article's precomputed embedding.
    Built once (see `build_rag_index`), then reused for every
    `/suggest` request for the lifetime of the process.
    """

    def __init__(self, embedder: Embedder, articles: list[_IndexedArticle]):
        self.embedder = embedder
        self.articles = articles


def seed_knowledge_base(db: Session) -> None:
    """
    Inserts the initial KB_ARTICLES content, but ONLY when the table is
    completely empty. This is deliberately simpler than an earlier
    version of this function, which detected "drift" against
    KB_ARTICLES (by comparing title sets) and actively reseeded the
    table to match - reasonable when KB_ARTICLES was the only way to
    add an article, but WRONG now that admins can create/edit/delete
    real articles through the UI (see main.py's /kb routes): that old
    logic would have silently deleted an admin's real work the next
    time the app started, the moment the DB's title set no longer
    matched the hardcoded Python list. KB_ARTICLES is now "initial demo
    content for a fresh database" only, not an ongoing source of truth.
    Embeddings aren't computed here - build_rag_index (below) always
    recomputes every article's embedding fresh, so a placeholder here
    is fine.
    """
    if db.query(KnowledgeArticle).first() is not None:
        return
    logger.info("Empty knowledge base - seeding %d initial articles", len(KB_ARTICLES))
    for article in KB_ARTICLES:
        row = KnowledgeArticle(title=article["title"], category=article["category"], content=article["content"])
        row.embedding = "[]"  # placeholder - build_rag_index recomputes this immediately after
        db.add(row)
    db.commit()


class IndexBuildError(RuntimeError):
    """The current (or candidate) knowledge base can't be turned into a
    usable retrieval index - e.g. every article is stopwords, or all
    articles are near-identical, leaving TF-IDF no vocabulary to fit."""


def build_rag_index(db: Session, persist: bool = True, seed: bool = True, commit: bool = True) -> RAGIndex:
    """
    Fits a fresh embedder on EVERY current article's content, and
    writes each article's recomputed embedding back to its row, every
    time this is called - not just at startup. Call it once at startup
    (see main.py's lifespan handler) AND after any knowledge-base write
    (create/update/delete - see main.py's /kb routes), so the live
    RAGIndex cached on app.state never drifts from what an admin
    actually saved.

    Always recomputing every embedding (not just new/changed rows) is
    deliberate, not wasteful for this KB's size (tens of articles,
    milliseconds to refit - already measured): the TF-IDF/SVD embedder
    must be fit on the WHOLE corpus at once, so an incrementally-added
    embedding would live in a different, incomparable vector space from
    the rest. This also fixes a real, previously-documented gap: the
    old title-based drift detection could miss a content-only edit
    (same title, changed body) - recomputing from current DB content
    every time makes that class of staleness structurally impossible,
    not just less likely.
    """
    """
    B4: `persist=False` builds a CANDIDATE index from the session's
    current (possibly uncommitted) state without writing or committing
    anything, so a KB change can be validated BEFORE it's committed.
    Raises IndexBuildError instead of leaking scikit-learn's ValueError.
    `seed=False` skips demo seeding, which commits and would otherwise
    commit a pending, unvalidated change along with it.
    """
    if seed:
        seed_knowledge_base(db)
    articles = db.query(KnowledgeArticle).all()

    embedder = Embedder()
    indexed: list[_IndexedArticle] = []
    if len(articles) >= 2:
        # TruncatedSVD needs at least 2 documents to fit a meaningful
        # space - see Embedder.fit's own guard. Fewer than that (an
        # admin deleted down to 0 or 1 articles) means suggestions
        # simply can't run yet; main.py's /kb delete route blocks
        # deleting below 2 articles for exactly this reason, but this
        # check stays here too as a real backstop, not just a UI nicety.
        try:
            embedder.fit([a.content for a in articles])
        except ValueError as exc:
            raise IndexBuildError(
                "The knowledge base doesn't contain enough distinct wording to build a search index "
                f"({exc}). Add more descriptive content to at least one article."
            ) from exc
        for a in articles:
            vector = embedder.embed(a.content)
            if persist:
                a.set_embedding(vector)
            indexed.append(_IndexedArticle(id=a.id, title=a.title, category=a.category, content=a.content, embedding=vector))
        if persist and commit:
            db.commit()
    else:
        logger.warning("Knowledge base has fewer than 2 articles (%d) - SupportRAG will abstain on everything until more are added", len(articles))

    logger.info("RAGIndex built: %d articles indexed", len(indexed))
    return RAGIndex(embedder=embedder, articles=indexed)


class SupportRAGService:
    """
    Thin, cheap, per-request wrapper around a pre-built RAGIndex. Holds
    no DB session and does no fitting - safe to construct on every
    request without a performance cost.
    """

    def __init__(self, index: RAGIndex, settings: Settings):
        self.index = index
        self.settings = settings
        self.top_k = settings.rag_top_k
        self.confidence_threshold = settings.rag_confidence_threshold

    def suggest(self, description: str, ticket_id: int | None = None) -> Suggestion:
        if not self.index.articles:
            logger.warning("suggest() called with an empty RAGIndex")
            return Suggestion(abstained=True, category=None, confidence=0.0,
                               draft_response="No knowledge base available - cannot suggest.")

        query_vec = self.index.embedder.embed(description)
        scored = [
            (article, cosine_similarity(query_vec, article.embedding))
            for article in self.index.articles
        ]
        scored.sort(key=lambda pair: pair[1], reverse=True)
        top = scored[:self.top_k]
        best_score = top[0][1]

        if best_score < self.confidence_threshold:
            logger.info(
                "suggestion abstained ticket_id=%s best_score=%.3f threshold=%.3f",
                ticket_id, best_score, self.confidence_threshold,
            )
            return Suggestion(
                abstained=True,
                category=None,
                confidence=round(best_score, 3),
                sources=[Source(a.id, a.title, a.category, round(s, 3)) for a, s in top],
                draft_response=(
                    "No knowledge-base article is a confident match for this "
                    "ticket. Recommend routing to a human agent for manual "
                    "triage rather than accepting an automated suggestion."
                ),
            )

        category_votes: dict[str, float] = defaultdict(float)
        for article, score in top:
            category_votes[article.category] += score
        best_category = max(category_votes, key=category_votes.get)

        sources = [Source(a.id, a.title, a.category, round(s, 3)) for a, s in top]
        draft, draft_source = self._draft_response(top, description)

        logger.info(
            "suggestion made ticket_id=%s category=%s confidence=%.3f sources=%s draft_source=%s",
            ticket_id, best_category, best_score, [a.id for a, _ in top], draft_source,
        )

        return Suggestion(
            abstained=False,
            category=best_category,
            confidence=round(best_score, 3),
            sources=sources,
            draft_response=draft,
            draft_source=draft_source,
        )

    def _draft_response(
        self, top: list[tuple[_IndexedArticle, float]], ticket_description: str,
    ) -> tuple[str, str]:
        """
        Returns (draft_text, draft_source) where draft_source is "llm" or
        "template". Tries the optional LLM rewrite first (see app/llm.py);
        any failure there - disabled, network error, bad response - falls
        back to the deterministic template so this never breaks the
        suggestion feature, just potentially makes it plainer.
        """
        best_article, _ = top[0]

        llm_draft = generate_grounded_draft(
            settings=self.settings,
            ticket_description=ticket_description,
            source_title=best_article.title,
            source_content=best_article.content,
        )
        if llm_draft:
            return llm_draft, "llm"

        return self._template_draft_response(top), "template"

    @staticmethod
    def _template_draft_response(top: list[tuple[_IndexedArticle, float]]) -> str:
        best_article, _ = top[0]
        other_titles = [a.title for a, _ in top[1:] if a.title != best_article.title]
        draft = f"Suggested response, based on \"{best_article.title}\":\n\n{best_article.content}"
        if other_titles:
            draft += f"\n\n(Also related: {', '.join(other_titles)})"
        draft += "\n\n[This is a drafted suggestion - review and edit before sending to the customer.]"
        return draft
