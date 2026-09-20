"""
Optional LLM step for SupportRAG's drafted response.

Talks to any OpenAI-compatible chat-completions endpoint (Groq by
default - see app/config.py for why). This module has exactly one job:
turn a retrieved KB article into a more naturally-worded draft, WITHOUT
letting the model invent anything the article didn't say. It never
decides the category, never sees tickets that got an abstain, and never
gets asked an open-ended question - the prompt is deliberately
close-ended ("rewrite this", not "help this customer").

Failure handling is the point of this module as much as the API call
is. Free-tier LLM providers are genuinely volatile - model names get
decommissioned, rate limits get hit, endpoints time out - and none of
that should ever be able to break ticket triage. Every failure mode
here returns None, and the caller (app/supportrag.py) falls back to the
existing deterministic template when that happens. This was a
deliberate design response to real research on free-tier LLM API
reliability, not a hypothetical concern.
"""
from __future__ import annotations

import logging

import requests

from app.config import Settings

logger = logging.getLogger("ticketbase.llm")

SYSTEM_PROMPT = (
    "You rewrite internal support knowledge-base excerpts into a short, "
    "clear reply a support agent can send to a customer. Rules: "
    "(1) Use ONLY the information in the provided excerpt - do not add "
    "any fact, step, or claim that isn't in it. "
    "(2) If the excerpt doesn't fully answer the ticket, say what it "
    "does cover rather than guessing at the rest. "
    "(3) Keep it under 120 words, plain sentences, no headers or bullet "
    "lists. (4) Do not mention that you are an AI or that this is based "
    "on an excerpt - just write the reply itself."
)


def generate_grounded_draft(
    settings: Settings,
    ticket_description: str,
    source_title: str,
    source_content: str,
) -> str | None:
    """
    Returns a rewritten draft grounded in `source_content`, or None if
    the LLM step is disabled or fails for any reason. Never raises -
    callers can treat None exactly like "LLM not configured".
    """
    if not settings.llm_api_key:
        return None

    user_prompt = (
        f"Customer's ticket: {ticket_description}\n\n"
        f"Relevant knowledge-base article (\"{source_title}\"):\n{source_content}\n\n"
        "Write the reply now."
    )

    try:
        response = requests.post(
            f"{settings.llm_api_base}/chat/completions",
            headers={
                "Authorization": f"Bearer {settings.llm_api_key}",
                "Content-Type": "application/json",
            },
            json={
                "model": settings.llm_model,
                "messages": [
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": user_prompt},
                ],
                "temperature": 0.3,
                "max_tokens": 220,
            },
            timeout=settings.llm_timeout_seconds,
        )
    except requests.exceptions.RequestException as exc:
        logger.warning("LLM request failed (network/timeout): %s", exc)
        return None

    if response.status_code != 200:
        # Covers rate limits (429), decommissioned/renamed models (404),
        # auth errors (401), and anything else - all treated the same
        # way: log it, fall back, keep the feature working.
        logger.warning(
            "LLM request returned %d, falling back to template. Body: %s",
            response.status_code, response.text[:300],
        )
        return None

    try:
        data = response.json()
        text = data["choices"][0]["message"]["content"].strip()
    except (KeyError, IndexError, ValueError) as exc:
        logger.warning("LLM response had unexpected shape: %s", exc)
        return None

    if not text:
        return None

    return text
