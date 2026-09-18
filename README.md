# TicketBase

A support ticket system: FastAPI backend + a CLI client. This is **Project 1**
of a two-project portfolio plan - a solid backend foundation with one
deliberately small, human-reviewed AI feature to be added later (Project 2,
SupportRAG, builds on top of this).

## Quick start

```bash
pip install -r requirements.txt
uvicorn app.main:app --reload
```

Visit `http://127.0.0.1:8000/docs` for interactive API docs.

Visit `http://127.0.0.1:8000/` for the web UI — create tickets, filter by
status, view details, confirm categories, and update status, all from
the browser.

In a second terminal, use the CLI:

```bash
python cli.py create "The VPN is down and I cannot access anything"
python cli.py list
python cli.py status 1 in_progress
python cli.py confirm-category 1 connectivity
```

Both the web UI and the CLI call the exact same underlying `crud.py`
functions - no logic is duplicated between them, only the presentation
differs.

## Run the tests

```bash
pytest -v
```

## Architecture

```
CLI (cli.py) ──HTTP──▶ FastAPI (app/main.py)
                            │
                            ▼
                       crud.py (data access)
                            │
                    ┌───────┴────────┐
                    ▼                ▼
              rules.py          models.py
          (deterministic      (SQLAlchemy,
           priority logic,      SQLite by
             no AI)           default, Postgres-
                                 ready)
```

**No AI in this project yet.** Priority is computed with plain keyword
matching (`app/rules.py`) - intentionally crude, intentionally explainable.
This is the baseline Project 2 will eventually need to beat.

## Data model

A ticket has a `category` field that starts `null` with `category_confirmed
= False`. The only way to set it is the explicit `PATCH /tickets/{id}/category`
endpoint. This isn't an accident - it's the structural hook that will let
Project 2 suggest a category without ever being able to silently apply one
without a human confirming it first.

## Non-goals (for now)

- No authentication - fine for local/portfolio use, not fine for anything real
- No AI category suggestion yet - Week 4+ addition
- No multi-user support
- Using SQLite by default - switch to Postgres (`docker-compose.yml` included)
  before starting Project 2, since that needs the pgvector extension

## Known limitations

- The priority rules in `app/rules.py` are crude keyword matching and will
  misclassify plenty of real tickets. That's expected - it exists to be a
  simple, explainable baseline, not a finished feature.
- `Base.metadata.create_all()` in `main.py` is fine for a project this size,
  but isn't how you'd manage schema changes in a real system - that's what
  Alembic migrations are for, intentionally left out of scope here.

## A debugging story, already lived

While writing the test suite, all 11 non-health-check tests failed with
`no such table: tickets` - despite `Base.metadata.create_all()` clearly
running in the test setup. The cause: SQLAlchemy opens a new database
connection per session by default, and for SQLite's `:memory:` database,
**every new connection gets its own separate, empty database.** The table
created in one connection was invisible to the next. Fix: force all test
sessions to share a single connection with `poolclass=StaticPool`. See the
comment in `tests/test_tickets.py` for the full explanation.

This is a legitimate "one failed approach and what I learned" story for
an interview - not a hypothetical one.

A second one, from building the web UI: `templates.TemplateResponse()`
threw `TypeError: unhashable type: 'dict'` on every page load. Cause:
Starlette 1.6.0 changed the expected call signature - `request` must now
be passed as the first positional argument (`TemplateResponse(request,
"name.html", {...})`), not buried inside the context dictionary
(`TemplateResponse("name.html", {"request": request, ...})`, the older,
widely-documented pattern). Caught by actually loading the page and
reading the real traceback, not by assuming the code was correct because
it "looked standard."

## Next steps (Project 2 preview)

- Swap to Postgres (`docker-compose.yml` is ready)
- Add `pgvector` for embeddings storage
- Add a `SupportRAG` service that suggests (never auto-applies) a category
  and drafts a response, with retrieval sources cited and an abstain path
  when retrieval confidence is low
