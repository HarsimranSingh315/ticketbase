"""
SupportRAG (Project 2): suggests a ticket category and drafts a response
by retrieving the most similar knowledge-base articles.

Hard rule carried over from Project 1's design: this module never writes
to a ticket's `category` / `category_confirmed` fields. It only returns a
Suggestion for a human to review. The only code path that can confirm a
category is still `crud.confirm_category()`, called from an explicit
human action (web UI button or CLI command) - see models.py and
main.py's confirm_ticket_category endpoint for where that's enforced.

Two failure modes this is designed to avoid:
1. A confident-sounding wrong answer with no way to check it -> every
   suggestion cites the specific KB articles it's based on, with their
   similarity scores, so a human can verify the reasoning, not just the
   conclusion.
2. Silently guessing on something outside the KB's coverage -> if the
   best match's similarity is below CONFIDENCE_THRESHOLD, this abstains
   instead of forcing a low-quality suggestion.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field

from sqlalchemy.orm import Session

from app.embeddings import Embedder, cosine_similarity
from app.kb_articles import KB_ARTICLES
from app.models import KnowledgeArticle

TOP_K = 3
CONFIDENCE_THRESHOLD = 0.25  # below this, abstain rather than guess


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


def seed_knowledge_base(db: Session, embedder: Embedder) -> None:
    """
    Populates knowledge_articles from KB_ARTICLES if the table is empty.
    Idempotent - safe to call on every app startup.
    """
    if db.query(KnowledgeArticle).first() is not None:
        return

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


class SupportRAGService:
    """
    One instance per request (or per script run) is fine - fitting the
    embedder on 16 short articles takes milliseconds. For a much larger
    KB, the embedder/vectorizer would be fit once at startup and cached,
    not refit per request.
    """

    def __init__(self, db: Session):
        self.db = db
        self.embedder = Embedder()
        articles = db.query(KnowledgeArticle).all()
        if not articles:
            seed_knowledge_base(db, self.embedder)
            articles = db.query(KnowledgeArticle).all()
        else:
            # Re-fit on existing article content so new queries land in
            # the same TF-IDF vocabulary space as the stored embeddings.
            self.embedder.fit([a.content for a in articles])
        self.articles = articles

    def suggest(self, description: str) -> Suggestion:
        if not self.articles:
            return Suggestion(abstained=True, category=None, confidence=0.0,
                               draft_response="No knowledge base available - cannot suggest.")

        query_vec = self.embedder.embed(description)
        scored = [
            (article, cosine_similarity(query_vec, article.get_embedding()))
            for article in self.articles
        ]
        scored.sort(key=lambda pair: pair[1], reverse=True)
        top = scored[:TOP_K]
        best_score = top[0][1]

        if best_score < CONFIDENCE_THRESHOLD:
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

        return Suggestion(
            abstained=False,
            category=best_category,
            confidence=round(best_score, 3),
            sources=sources,
            draft_response=draft,
        )

    @staticmethod
    def _draft_response(top: list[tuple[KnowledgeArticle, float]]) -> str:
        best_article, _ = top[0]
        other_titles = [a.title for a, _ in top[1:] if a.title != best_article.title]
        draft = f"Suggested response, based on \"{best_article.title}\":\n\n{best_article.content}"
        if other_titles:
            draft += f"\n\n(Also related: {', '.join(other_titles)})"
        draft += "\n\n[This is a drafted suggestion - review and edit before sending to the customer.]"
        return draft
