"""
Related tickets: "has this happened before?" without an agent having to
manually search.

Real ticketing tools (Zendesk and similar) leave this as manual work -
an agent has to think to search, then search well, then read through
results themselves. This surfaces it automatically on every ticket.

Deliberately reuses the SAME fitted embedder that SupportRAG already
builds for the knowledge base (see supportrag.RAGIndex) rather than
fitting a second one. Two reasons: (1) no second TF-IDF vocabulary to
maintain and keep in sync, (2) the KB embedder's vocabulary already
covers real support terminology (VPN, billing, outage, etc.), which is
exactly the vocabulary ticket descriptions use too. The tradeoff is the
same one already documented for SupportRAG: TF-IDF matches shared
vocabulary, not deep meaning - acceptable here for the same reasons.

Unlike the knowledge base (16 fixed articles, embedded once at
startup), tickets are created continuously, so there's no fixed corpus
to pre-embed. This computes embeddings for the candidate tickets ON THE
FLY, per request. That's fine at portfolio scale (see
crud.list_other_tickets's cap) but is a real scaling limit worth naming
rather than hiding: a high-volume deployment would want to store each
ticket's embedding at creation time (same pattern as
KnowledgeArticle.embedding) instead of recomputing it on every related-
tickets lookup - see the Next steps section in README.md.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

from app.embeddings import Embedder, cosine_similarity
from app.models import Ticket

logger = logging.getLogger("ticketbase.related_tickets")

TOP_K = 3
SIMILARITY_THRESHOLD = 0.3


@dataclass
class RelatedTicket:
    id: int
    description: str
    status: str
    similarity: float


def find_related_tickets(
    embedder: Embedder,
    target_description: str,
    candidates: list[Ticket],
    top_k: int = TOP_K,
    threshold: float = SIMILARITY_THRESHOLD,
) -> list[RelatedTicket]:
    """
    Returns up to `top_k` tickets from `candidates` whose description is
    similar to `target_description`, above `threshold`. Empty list is a
    valid, expected result - not every ticket has a predecessor, and
    this doesn't force a weak match the way abstaining in SupportRAG
    avoids the same problem for category suggestions.

    `embedder` must already be fitted (the RAGIndex one always is).
    """
    if not candidates:
        return []

    target_vec = embedder.embed(target_description)

    scored = []
    for ticket in candidates:
        candidate_vec = embedder.embed(ticket.description)
        similarity = cosine_similarity(target_vec, candidate_vec)
        if similarity >= threshold:
            scored.append((ticket, similarity))

    scored.sort(key=lambda pair: pair[1], reverse=True)
    top = scored[:top_k]

    logger.info(
        "related tickets: %d candidates, %d above threshold %.2f, returning %d",
        len(candidates), len(scored), threshold, len(top),
    )

    return [
        RelatedTicket(id=t.id, description=t.description, status=t.status.value, similarity=round(s, 3))
        for t, s in top
    ]
