"""
Mail sending adapters.

Two implementations behind one interface:
- LocalSinkAdapter (default, no config needed): nothing ever leaves the
  machine. "Sending" writes a row to local_sink_emails, which is
  genuinely idempotent (see below) - built and fully tested, this is
  what every test and local demo actually uses.
- ResendAdapter (only when RESEND_API_KEY is set): a real provider
  integration. Written and covered by mocked unit tests, but NOT
  verified against a live Resend account in this project - there is no
  key available here, same situation and same honesty as app/llm.py's
  Groq integration before a real key existed for it. If you configure
  a real key, test it yourself before trusting it in anger.

Both adapters take an `idempotency_key` and are expected to be safe to
call twice with the same key - this is the actual mechanism that
prevents a double-send when a worker retries a job it's not sure
completed (see worker.py and crud.claim_next_job's docstring).
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional, Protocol

import requests
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.config import Settings
from app.models import LocalSinkEmail

logger = logging.getLogger("ticketbase.mail")


@dataclass
class MailResult:
    success: bool
    provider_message_id: Optional[str] = None
    error: Optional[str] = None


class MailAdapter(Protocol):
    def send(self, recipient: str, subject: str, body: str, idempotency_key: str) -> MailResult: ...


class LocalSinkAdapter:
    """
    The default adapter. Requires a DB session because "sending" here
    just means durably recording the email - there is no external
    network call, so there is no separate "delivery" concept: local
    sink mail is always immediately and permanently "sent" from this
    adapter's point of view (still subject to the SAME approval/outbox
    workflow as a real send - this only replaces the very last step).
    """

    def __init__(self, db: Session):
        self.db = db

    def send(self, recipient: str, subject: str, body: str, idempotency_key: str) -> MailResult:
        existing = self.db.query(LocalSinkEmail).filter(LocalSinkEmail.idempotency_key == idempotency_key).first()
        if existing is not None:
            # Already sent under this exact key - this IS the
            # idempotency guarantee, not just a nice-to-have: a worker
            # that crashed after a successful send but before marking
            # the job complete will retry, land here, and get told
            # "already done" instead of sending a second copy.
            logger.info("local sink: idempotency_key %s already sent, not duplicating", idempotency_key)
            return MailResult(success=True, provider_message_id=f"local-{existing.id}")

        row = LocalSinkEmail(
            idempotency_key=idempotency_key, recipient_email=recipient, subject=subject, body=body,
        )
        self.db.add(row)
        try:
            self.db.commit()
        except IntegrityError:
            # A genuine race: two callers hit send() with the same key
            # at almost the same instant, both passed the check above,
            # and the UNIQUE constraint on idempotency_key is what
            # actually decides it - one wins, one lands here. Treat the
            # loser the same as "already sent", which is correct: it is.
            self.db.rollback()
            existing = self.db.query(LocalSinkEmail).filter(LocalSinkEmail.idempotency_key == idempotency_key).first()
            return MailResult(success=True, provider_message_id=f"local-{existing.id}" if existing else None)

        self.db.refresh(row)
        logger.info("local sink: captured email to %s (key=%s)", recipient, idempotency_key)
        return MailResult(success=True, provider_message_id=f"local-{row.id}")


class ResendAdapter:
    """
    Real Resend integration (https://resend.com). Uses Resend's own
    Idempotency-Key header, so the "don't double-send on retry"
    guarantee comes from the provider itself, not just this adapter's
    logic - but Resend's documented idempotency window is 24 hours; a
    retry attempted after that window is NOT guaranteed deduplicated by
    Resend, and this adapter does not implement its own additional
    tracking for that edge - a real gap, named here rather than implied
    to be handled.
    """

    def __init__(self, settings: Settings):
        self.settings = settings

    def send(self, recipient: str, subject: str, body: str, idempotency_key: str) -> MailResult:
        try:
            response = requests.post(
                "https://api.resend.com/emails",
                headers={
                    "Authorization": f"Bearer {self.settings.resend_api_key}",
                    "Content-Type": "application/json",
                    "Idempotency-Key": idempotency_key,
                },
                json={
                    "from": f"{self.settings.mail_from_name} <{self.settings.mail_from_address}>",
                    "to": [recipient],
                    "subject": subject,
                    "text": body,
                },
                timeout=15,
            )
        except requests.exceptions.RequestException as exc:
            logger.warning("Resend request failed: %s", exc)
            return MailResult(success=False, error=str(exc))

        if response.status_code not in (200, 201):
            logger.warning("Resend returned %d: %s", response.status_code, response.text[:300])
            return MailResult(success=False, error=f"HTTP {response.status_code}: {response.text[:300]}")

        try:
            data = response.json()
            provider_id = data["id"]
        except (ValueError, KeyError) as exc:
            logger.warning("Resend response had unexpected shape: %s", exc)
            return MailResult(success=False, error=f"unexpected response shape: {exc}")

        return MailResult(success=True, provider_message_id=provider_id)


def get_mail_adapter(settings: Settings, db: Session) -> MailAdapter:
    if settings.resend_api_key:
        return ResendAdapter(settings)
    return LocalSinkAdapter(db)
