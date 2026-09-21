"""
TicketBase SLA breach checker.

A separate, TIME-based periodic process - deliberately distinct from
worker.py's QUEUE-based outbox processing, not a variant of it. These
are genuinely different architectures for genuinely different
problems: the outbox worker claims and processes individual jobs as
they appear; this one periodically re-scans ALL open tickets and checks
each against its own SLA deadline. Conflating them into one process
would mean either the outbox polling interval dictates how often SLAs
get checked (or vice versa) for no real reason - keeping them separate
processes keeps each one's polling interval independently tunable.

Run it with:
    python sla_check.py            # runs continuously, checking on an interval
    python sla_check.py --once     # runs exactly one check cycle, then exits
                                    # (used by tests, and useful for manual
                                    # debugging)
"""
import argparse
import logging
import time

from app.config import get_settings
from app.database import SessionLocal
from app import crud

logging.basicConfig(
    level="INFO",
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger("ticketbase.sla_check")


def _sla_hours_from_settings(settings) -> dict:
    return {
        "high": settings.sla_high_priority_hours,
        "medium": settings.sla_medium_priority_hours,
        "low": settings.sla_low_priority_hours,
    }


def run_one_check() -> int:
    """Runs exactly one check cycle: finds newly-breached tickets and
    records an audit event for each. Returns the count recorded (0 is
    the normal, expected result most of the time). Split out from the
    polling loop, same reason as worker.py's run_one_cycle: tests can
    call this directly and deterministically instead of racing a
    background loop."""
    settings = get_settings()
    sla_hours = _sla_hours_from_settings(settings)
    db = SessionLocal()
    try:
        newly_recorded = crud.record_new_sla_breaches(db, sla_hours)
        if newly_recorded:
            logger.warning("Recorded %d newly-breached ticket(s)", newly_recorded)
        else:
            logger.info("SLA check complete - no new breaches")
        return newly_recorded
    finally:
        db.close()


def run_forever(poll_interval: float) -> None:
    logger.info("SLA checker starting, polling every %.1fs", poll_interval)
    while True:
        try:
            run_one_check()
        except Exception:
            # Same principle as worker.py: one bad cycle must never take
            # down the whole process and silently stop checking everything
            # else forever.
            logger.exception("unexpected error in SLA check cycle")
        time.sleep(poll_interval)


def main():
    parser = argparse.ArgumentParser(description="TicketBase SLA breach checker")
    parser.add_argument("--once", action="store_true", help="Run exactly one check cycle, then exit.")
    args = parser.parse_args()

    settings = get_settings()
    if args.once:
        newly_recorded = run_one_check()
        logger.info("--once: recorded %d new breach(es)", newly_recorded)
    else:
        run_forever(settings.sla_check_poll_interval_seconds)


if __name__ == "__main__":
    main()
