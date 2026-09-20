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

from app.config import get_settings
from app.embeddings import Embedder, cosine_similarity
from app.kb_articles import KB_ARTICLES
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


def seed_knowledge_base(db: Session, embedder: Embedder) -> None:
    """
    Populates knowledge_articles from KB_ARTICLES if the table is empty.
    Idempotent - safe to call on every app startup.
    """
    if db.query(KnowledgeArticle).first() is not None:
        return

    logger.info("Seeding knowledge base with %d articles", len(KB_ARTICLES))
    embedder.fit([a["content"] for a in KB_ARTICLES])
    for article in KB_ARTICLES:
        row = KnowledgeArticle(
            title=article["title"],
            category=article["category"],
            content=article["content"],
        )
        row.set_embedding(embedder.embed(article["content"]))
        db.add(row)
    db.commit()


def build_rag_index(db: Session) -> RAGIndex:
    """
    Fits the embedder on the current KB and snapshots every article's
    embedding into memory. Call this once at startup (or whenever the KB
    changes) - not per request.
    """
    embedder = Embedder()
    articles = db.query(KnowledgeArticle).all()
    if not articles:
        seed_knowledge_base(db, embedder)
        articles = db.query(KnowledgeArticle).all()
    else:
        embedder.fit([a.content for a in articles])

    indexed = [
        _IndexedArticle(id=a.id, title=a.title, category=a.category,
                         content=a.content, embedding=a.get_embedding())
        for a in articles
    ]
    logger.info("RAGIndex built: %d articles indexed", len(indexed))
    return RAGIndex(embedder=embedder, articles=indexed)


class SupportRAGService:
    """
    Thin, cheap, per-request wrapper around a pre-built RAGIndex. Holds
    no DB session and does no fitting - safe to construct on every
    request without a performance cost.
    """

    def __init__(self, index: RAGIndex):
        self.index = index
        settings = get_settings()
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
        draft = self._draft_response(top)

        logger.info(
            "suggestion made ticket_id=%s category=%s confidence=%.3f sources=%s",
            ticket_id, best_category, best_score, [a.id for a, _ in top],
        )

        return Suggestion(
            abstained=False,
            category=best_category,
            confidence=round(best_score, 3),
            sources=sources,
            draft_response=draft,
        )

    @staticmethod
    def _draft_response(top: list[tuple[_IndexedArticle, float]]) -> str:
        best_article, _ = top[0]
        other_titles = [a.title for a, _ in top[1:] if a.title != best_article.title]
        draft = f"Suggested response, based on \"{best_article.title}\":\n\n{best_article.content}"
        if other_titles:
            draft += f"\n\n(Also related: {', '.join(other_titles)})"
        draft += "\n\n[This is a drafted suggestion - review and edit before sending to the customer.]"
        return draft
