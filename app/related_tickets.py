"""
Related tickets: other tickets that describe a similar problem.

How matching works, and why (measured on tests/eval/related_pairs.json - 14
hand-labelled related pairs, 16 unrelated including hard negatives that share
a word but are different problems):

- A word-level TF-IDF vocabulary is built from the tickets being compared
  (the target plus the candidate pool), English stop words removed,
  sublinear term frequency. Cosine similarity of those vectors is the score.

- It previously reused the knowledge-base embedder (TF-IDF fit on KB
  articles only, compressed to 8 LSA dimensions). Two measured failures:
    1. Words absent from the KB vanished: "Outlook won't open, crashes on
       startup" embedded to an empty vector and could never match its
       obvious twin.
    2. The 8-dimension compression merged unrelated topics: a VPN ticket and
       a printer ticket with ZERO words in common scored 0.56.
  At the (unchanged) 0.30 threshold the old method matched 9/14 real pairs
  and wrongly matched 5/16 unrelated ones; this method matches 10/14 with
  0/16 false matches. The threshold was NOT tuned to the eval set - 0.30 is
  the pre-existing value - which limits overfitting on a set this small.

- Trade-off, stated plainly: word matching misses paraphrases that share
  few words ("will not connect from home" vs "keeps disconnecting, cannot
  reach the office network" share only "vpn"). Re-evaluated under
  production-like conditions (small candidate pools) on 15 related / 16
  unrelated pairs: 10/15 found, 0/16 false at 0.30. Character n-grams and a
  KB-background vocabulary were also tried; neither beat this. Closing the
  gap needs semantic embeddings - a model dependency and a decision about
  where text is processed, deliberately not taken here.
  For a panel agents glance at, a wrong suggestion costs more than a missed
  one, so precision is favoured.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.metrics.pairwise import cosine_similarity

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
    target_description: str,
    candidates: list[Ticket],
    top_k: int = TOP_K,
    threshold: float = SIMILARITY_THRESHOLD,
) -> list[RelatedTicket]:
    """Up to `top_k` candidates scoring at least `threshold`, best first.
    An empty list is a normal answer, not an error."""
    if not candidates or not (target_description or "").strip():
        return []
    texts = [target_description] + [t.description or "" for t in candidates]
    vectorizer = TfidfVectorizer(stop_words="english", sublinear_tf=True)
    try:
        matrix = vectorizer.fit_transform(texts)
    except ValueError:
        # Every text was stop words or empty - nothing meaningful to compare.
        return []
    scores = cosine_similarity(matrix[0], matrix[1:])[0]
    scored = sorted(
        ((t, float(s)) for t, s in zip(candidates, scores) if s >= threshold),
        key=lambda pair: pair[1], reverse=True,
    )
    top = scored[:top_k]
    logger.info("related tickets: %d candidates, %d above threshold %.2f, returning %d",
                len(candidates), len(scored), threshold, len(top))
    return [RelatedTicket(id=t.id, description=t.description, status=t.status.value, similarity=round(s, 3))
            for t, s in top]
