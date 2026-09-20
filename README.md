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

## Project 2: SupportRAG (now implemented)

Given a ticket, `SupportRAGService.suggest()` retrieves the most similar
articles from a small seeded knowledge base (`app/kb_articles.py`),
suggests a category, and drafts a response - citing exactly which
articles it's based on and how similar each one was. It never writes to
a ticket; `POST /tickets/{id}/suggest` is read-only, and applying a
suggestion still requires the same explicit `PATCH /tickets/{id}/category`
call as before (see `app/models.py` for why that's structurally
enforced, not just a convention). Available via the API, the CLI
(`python cli.py suggest <id>`), and the web UI (a "Get AI suggestion"
button on each ticket's page).

**Embeddings: TF-IDF + SVD, not a neural embedding model.** A real
sentence-embedding model (sentence-transformers, OpenAI/Anthropic
embeddings, etc.) needs a multi-hundred-MB deep learning stack and,
usually, an external API call. For a small fixed knowledge base like
this one, TF-IDF (word-frequency vectors) reduced with SVD gets
meaningful semantic matching - e.g. "VPN will not connect" correctly
retrieves the VPN and Wi-Fi articles over unrelated ones - with zero
external dependencies or API costs. This was a deliberate, documented
tradeoff (see `app/embeddings.py`), not a corner cut silently, and the
`Embedder` class is the only thing that would need to change to swap in
real neural embeddings later.

**Postgres + pgvector: scaffolded, not required.** `docker-compose.yml`
already provisions Postgres. `KnowledgeArticle.embedding` is stored as a
JSON-encoded list of floats in a portable `Text` column rather than a
native `pgvector` column, so the whole project still runs on plain
SQLite with zero setup - see the docstring in `app/models.py` for
exactly what the production swap to a native `Vector` column (and an
indexed `ORDER BY embedding <=> query_embedding` instead of the Python
`cosine_similarity()` loop in `embeddings.py`) would involve.

**A known limitation, honestly:** with a 16-article knowledge base and
TF-IDF's small vocabulary, similarity scores can be noisy for the
lower-ranked matches - e.g. a VPN query's #3 source came back as a
printer article at 0.84 similarity, purely from shared generic words
like "check" and "connection". This didn't change the final category
(the top two connectivity-related sources outweighed it), but it's a
real limitation worth naming rather than hiding: TF-IDF matches
vocabulary overlap, not true meaning, and a bigger/more diverse KB or
real embeddings would reduce this noise.

**A third debugging story, from writing this feature's tests:** running
`test_tickets.py` and `test_supportrag.py` together failed with `no such
table: tickets` - the exact same symptom as the original SQLite bug
above, but a different cause. Each file defined its own
`app.dependency_overrides[get_db] = override_get_db` at module import
time. Pytest imports every test file before running any test, so
whichever file was imported last silently overwrote the other's
override for the entire session - meaning one file's requests could hit
the other file's separate in-memory database, one that its own
`create_all()` fixture had never touched. Fixed by moving the engine,
session override, and `client` fixture into a single shared
`tests/conftest.py`. See that file's docstring for the full writeup -
this is a second genuine "found a real bug via a confusing failure,
diagnosed the actual cause" story for an interview, distinct from the
first one.

## Next steps

- Swap `DATABASE_URL` to the Postgres URL and run `docker-compose up -d`
  to develop against Postgres instead of SQLite
- Migrate `KnowledgeArticle.embedding` to a native `pgvector` column and
  push similarity search into the database
- Grow the knowledge base beyond 16 seed articles (ideally from real
  resolved tickets) to reduce the TF-IDF noise described above
- Optionally add a generative step (an LLM call) that rewrites the
  drafted response in a more natural voice, while keeping the same
  cited-sources-and-abstain-path structure - the retrieval layer this
  version built is what makes that safe to add later
