"""
TicketBase CLI.

This talks to the running API over HTTP - it is NOT a shortcut that
bypasses the API. That matters: it proves the API itself is a real,
usable interface, not just plumbing for a UI that happens to sit on top
of it. Anyone (a future web frontend, a script, another service) could
talk to the same API the same way this CLI does.

Usage (make sure `uvicorn app.main:app --reload` is running first):

    python cli.py create "The VPN is down and I cannot access anything"
    python cli.py list
    python cli.py list --status open
    python cli.py get 1
    python cli.py status 1 in_progress
    python cli.py confirm-category 1 connectivity
"""
import click
import requests

API_BASE = "http://127.0.0.1:8000"


def _print_ticket(t: dict):
    click.echo(f"#{t['id']:<4} [{t['priority']:<6}] [{t['status']:<11}] {t['description']}")
    if t["category"]:
        confirmed = "confirmed" if t["category_confirmed"] else "suggested, unconfirmed"
        click.echo(f"       category: {t['category']} ({confirmed})")


@click.group()
def cli():
    """TicketBase - a support ticket system, CLI edition."""
    pass


@cli.command()
@click.argument("description")
def create(description):
    """Create a new ticket."""
    resp = requests.post(f"{API_BASE}/tickets", json={"description": description})
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
    resp = requests.get(f"{API_BASE}/tickets", params=params)
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
    resp = requests.get(f"{API_BASE}/tickets/{ticket_id}")
    if resp.status_code == 404:
        click.echo(f"Ticket #{ticket_id} not found.")
        return
    resp.raise_for_status()
    _print_ticket(resp.json())


@cli.command()
@click.argument("ticket_id", type=int)
@click.argument("new_status")
def status(ticket_id, new_status):
    """Update a ticket's status: open, in_progress, or resolved."""
    resp = requests.patch(f"{API_BASE}/tickets/{ticket_id}/status", json={"status": new_status})
    if resp.status_code == 404:
        click.echo(f"Ticket #{ticket_id} not found.")
        return
    resp.raise_for_status()
    click.echo(f"Ticket #{ticket_id} status updated.")
    _print_ticket(resp.json())


@cli.command(name="confirm-category")
@click.argument("ticket_id", type=int)
@click.argument("category")
def confirm_category(ticket_id, category):
    """Confirm (or set) a ticket's category. This is the human-review step."""
    resp = requests.patch(f"{API_BASE}/tickets/{ticket_id}/category", json={"category": category})
    if resp.status_code == 404:
        click.echo(f"Ticket #{ticket_id} not found.")
        return
    resp.raise_for_status()
    click.echo(f"Ticket #{ticket_id} category confirmed.")
    _print_ticket(resp.json())


if __name__ == "__main__":
    cli()
