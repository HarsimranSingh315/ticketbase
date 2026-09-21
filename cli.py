"""
TicketBase CLI.

This talks to the running API over HTTP - it is NOT a shortcut that
bypasses the API. That matters: it proves the API itself is a real,
usable interface, not just plumbing for a UI that happens to sit on top
of it. Anyone (a future web frontend, a script, another service) could
talk to the same API the same way this CLI does.

Configuration, all via environment variables (no hardcoded values, same
principle as app/config.py for the server itself):
    TICKETBASE_API_URL   - base URL of the running API (default: http://127.0.0.1:8000)
    TICKETBASE_API_KEY   - sent as the X-API-Key header, if the server has API_KEY set
    TICKETBASE_TIMEOUT   - request timeout in seconds (default: 10)

Usage (make sure `uvicorn app.main:app --reload` is running first):

    python cli.py create "The VPN is down and I cannot access anything"
    python cli.py list
    python cli.py list --status open
    python cli.py get 1
    python cli.py status 1 in_progress
    python cli.py confirm-category 1 connectivity
"""
import os
import sys

import click
import requests

API_BASE = os.environ.get("TICKETBASE_API_URL", "http://127.0.0.1:8000")
API_KEY = os.environ.get("TICKETBASE_API_KEY", "")
TIMEOUT = float(os.environ.get("TICKETBASE_TIMEOUT", "10"))


def _request(method: str, path: str, **kwargs):
    """
    Every CLI command routes through here - the one place that knows
    about the base URL, the auth header, and the timeout, so none of
    that is repeated (or forgotten) per-command. Also the one place
    that turns a 401 (bad/missing API key) and a network failure into a
    clear message instead of a raw traceback - a CLI user shouldn't
    need to read a requests stack trace to learn "your key is wrong".
    """
    headers = kwargs.pop("headers", {})
    if API_KEY:
        headers["X-API-Key"] = API_KEY
    url = f"{API_BASE}{path}"
    try:
        resp = requests.request(method, url, headers=headers, timeout=TIMEOUT, **kwargs)
    except requests.exceptions.ConnectionError:
        click.echo(f"Could not reach {API_BASE} - is the server running?", err=True)
        sys.exit(1)
    except requests.exceptions.Timeout:
        click.echo(f"Request to {url} timed out after {TIMEOUT}s.", err=True)
        sys.exit(1)

    if resp.status_code == 401:
        click.echo(
            "401 Unauthorized - the server requires an API key. "
            "Set TICKETBASE_API_KEY to match the server's API_KEY setting.",
            err=True,
        )
        sys.exit(1)
    return resp


def _print_ticket(t: dict):
    click.echo(f"#{t['id']:<4} [{t['priority']:<6}] [{t['status']:<11}] {t['description']}")
    if t["category"]:
        confirmed = "confirmed" if t["category_confirmed"] else "suggested, unconfirmed"
        click.echo(f"       category: {t['category']} ({confirmed})")


def _get_ticket_or_exit(ticket_id: int) -> dict:
    """Fetches the ticket fresh - needed before any write, since writes
    now require the ticket's current `version` (optimistic concurrency -
    see crud.VersionConflict). Exits cleanly if the ticket doesn't exist,
    rather than letting a KeyError on a 404 body leak out."""
    resp = _request("GET", f"/tickets/{ticket_id}")
    if resp.status_code == 404:
        click.echo(f"Ticket #{ticket_id} not found.")
        sys.exit(1)
    resp.raise_for_status()
    return resp.json()


@click.group()
def cli():
    """TicketBase - a support ticket system, CLI edition."""
    pass


@cli.command()
@click.argument("description")
def create(description):
    """Create a new ticket."""
    resp = _request("POST", "/tickets", json={"description": description})
    resp.raise_for_status()
    ticket = resp.json()
    click.echo(f"Created ticket #{ticket['id']}")
    _print_ticket(ticket)


@cli.command(name="list")
@click.option("--status", default=None, help="Filter by status: open, in_progress, resolved")
@click.option("--priority", default=None, help="Filter by priority: low, medium, high")
def list_cmd(status, priority):
    """List tickets, optionally filtered."""
    params = {k: v for k, v in {"status": status, "priority": priority}.items() if v}
    resp = _request("GET", "/tickets", params=params)
    resp.raise_for_status()
    tickets = resp.json()
    if not tickets:
        click.echo("No tickets found.")
        return
    for t in tickets:
        _print_ticket(t)


@cli.command()
@click.argument("ticket_id", type=int)
def get(ticket_id):
    """Show one ticket in detail."""
    resp = _request("GET", f"/tickets/{ticket_id}")
    if resp.status_code == 404:
        click.echo(f"Ticket #{ticket_id} not found.")
        return
    resp.raise_for_status()
    _print_ticket(resp.json())


@cli.command()
@click.argument("ticket_id", type=int)
@click.argument("new_status")
def status(ticket_id, new_status):
    """
    Update a ticket's status: open, in_progress, or resolved.

    Fetches the ticket first to get its current `version` (writes now
    require it - see crud.VersionConflict): if someone else changed the
    ticket between your last look and this command, the server rejects
    a stale write with 409 rather than silently overwriting it. Run
    the command again to retry against the current state.
    """
    current = _get_ticket_or_exit(ticket_id)
    resp = _request(
        "PATCH", f"/tickets/{ticket_id}/status",
        json={"status": new_status, "version": current["version"]},
    )
    if resp.status_code == 409:
        click.echo(f"Ticket #{ticket_id} changed since it was last read - run the command again.", err=True)
        sys.exit(1)
    resp.raise_for_status()
    click.echo(f"Ticket #{ticket_id} status updated.")
    _print_ticket(resp.json())


@cli.command(name="confirm-category")
@click.argument("ticket_id", type=int)
@click.argument("category")
def confirm_category(ticket_id, category):
    """Confirm (or set) a ticket's category. This is the human-review step.
    See `status`'s docstring for why this fetches the ticket first."""
    current = _get_ticket_or_exit(ticket_id)
    resp = _request(
        "PATCH", f"/tickets/{ticket_id}/category",
        json={"category": category, "version": current["version"]},
    )
    if resp.status_code == 409:
        click.echo(f"Ticket #{ticket_id} changed since it was last read - run the command again.", err=True)
        sys.exit(1)
    resp.raise_for_status()
    click.echo(f"Ticket #{ticket_id} category confirmed.")
    _print_ticket(resp.json())


@cli.command()
@click.argument("ticket_id", type=int)
def suggest(ticket_id):
    """
    Get a SupportRAG category suggestion + drafted response for a
    ticket. Read-only - does NOT confirm anything. Run
    'confirm-category' afterwards if you agree with the suggestion.
    """
    resp = _request("POST", f"/tickets/{ticket_id}/suggest")
    if resp.status_code == 404:
        click.echo(f"Ticket #{ticket_id} not found.")
        return
    resp.raise_for_status()
    s = resp.json()

    if s["abstained"]:
        click.echo(f"SupportRAG abstained (confidence {s['confidence']}) — no confident match.")
        click.echo(s["draft_response"])
        return

    click.echo(f"Suggested category: {s['category']}  (confidence {s['confidence']})")
    click.echo("Sources:")
    for src in s["sources"]:
        click.echo(f"  - \"{src['title']}\" [{src['category']}]  similarity={src['similarity']}")
    source_label = "AI-written" if s.get("draft_source") == "llm" else "templated"
    click.echo(f"\nDrafted response ({source_label}):")
    click.echo(s["draft_response"])
    click.echo(f"\nTo accept: python cli.py confirm-category {ticket_id} {s['category']}")


if __name__ == "__main__":
    cli()
