"""
TicketBase outbox worker.

A real second process, separate from the web app (`uvicorn app.main:app`)
- run it with:

    python worker.py            # runs continuously, polling for jobs
    python worker.py --once     # processes at most one job, then exits
                                 # (used by tests, and useful for manual
                                 # debugging - "run exactly one cycle")

This is the piece that actually sends approved messages. It claims jobs
from the outbox_jobs table (see crud.claim_next_job - a compare-and-swap
UPDATE, portable across SQLite and Postgres, crash-safe via lease
expiry), calls the configured mail adapter (app/mail.py), and records
the result. Bounded retries with backoff on failure; a job that
exhausts its attempts becomes terminally `failed`, not retried forever.

Why a separate process rather than a background task inside the web
app: the brief's own architecture guidance - "a modular application
plus one worker process" - and a real, practical reason: a slow or
stuck mail provider should never be able to block or slow down request
handling in the web app. They're decoupled by the outbox table itself,
not by any shared in-process state.
"""
import argparse
import logging
import socket
import time
import uuid

from app.config import get_settings
from app.database import SessionLocal
from app import crud
from app.mail import get_mail_adapter

logging.basicConfig(
    level="INFO",
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger("ticketbase.worker")


def _worker_id() -> str:
    """A reasonably unique identifier for this worker process, used as
    the lease owner - lets you tell, from the outbox_jobs table, which
    process holds which lease. Not a security boundary, just
    diagnostics."""
    return f"{socket.gethostname()}-{uuid.uuid4().hex[:8]}"


def run_one_cycle(worker_id: str) -> bool:
    """
    Claims and processes at most one job. Returns True if a job was
    claimed (regardless of whether it succeeded or failed), False if
    there was nothing to do. Split out from the polling loop so tests
    can call it directly and deterministically, instead of racing a
    background loop.
    """
    settings = get_settings()
    db = SessionLocal()
    try:
        job = crud.claim_next_job(db, worker_id, lease_seconds=settings.outbox_lease_seconds)
        if job is None:
            return False

        message = crud.get_message(db, job.message_id)
        if message is None:
            # Shouldn't happen (message_id is a real FK), but a worker
            # must never crash on unexpected state - fail the job
            # loudly instead.
            logger.error("job %s references missing message %s", job.id, job.message_id)
            crud.fail_job(db, job.id, "referenced message no longer exists", backoff_seconds=0)
            return True

        adapter = get_mail_adapter(settings, db)
        # Send exactly the APPROVED snapshot, never the live (possibly
        # since-edited-in-theory, though editing an approved message is
        # blocked) fields - this is the "immutable approved snapshot"
        # guarantee actually being honored at send time, not just
        # stored.
        result = adapter.send(
            recipient=message.approved_recipient_email,
            subject=message.approved_subject,
            body=message.approved_body,
            idempotency_key=job.operation_key,
        )

        if result.success:
            crud.complete_job(db, job.id, result.provider_message_id)
            logger.info(
                "sent message %s (job %s) to %s, provider_id=%s",
                message.id, job.id, message.approved_recipient_email, result.provider_message_id,
            )
        else:
            # Simple exponential backoff, capped - attempts is already
            # incremented by claim_next_job at claim time.
            backoff = min(2 ** job.attempts, 300)
            crud.fail_job(db, job.id, result.error or "unknown error", backoff_seconds=backoff)
            logger.warning(
                "send failed for message %s (job %s), attempt %d/%d: %s",
                message.id, job.id, job.attempts, job.max_attempts, result.error,
            )
        return True
    finally:
        db.close()


def run_forever(worker_id: str, poll_interval: float) -> None:
    logger.info("worker %s starting, polling every %.1fs", worker_id, poll_interval)
    while True:
        try:
            found_work = run_one_cycle(worker_id)
        except Exception:
            # A worker that crashes on one bad job takes down the
            # whole process and stops sending everything else - never
            # acceptable. Log it, keep the loop alive.
            logger.exception("unexpected error in worker cycle")
            found_work = False
        if not found_work:
            time.sleep(poll_interval)


def main():
    parser = argparse.ArgumentParser(description="TicketBase outbox worker")
    parser.add_argument("--once", action="store_true", help="Process at most one job, then exit.")
    args = parser.parse_args()

    worker_id = _worker_id()
    settings = get_settings()

    if args.once:
        found_work = run_one_cycle(worker_id)
        logger.info("--once: %s", "processed one job" if found_work else "no work found")
    else:
        run_forever(worker_id, settings.outbox_poll_interval_seconds)


if __name__ == "__main__":
    main()
