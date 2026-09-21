# TicketBase

A support ticket system: FastAPI backend + a CLI client. This is **Project 1**
of a two-project portfolio plan - a solid backend foundation with one
deliberately small, human-reviewed AI feature to be added later (Project 2,
SupportRAG, builds on top of this).

## Quick start

```bash
pip install -r requirements.txt
cp .env.example .env        # optional - defaults work with no .env at all
alembic upgrade head        # applies the versioned schema migration
uvicorn app.main:app --reload
```

Visit `http://127.0.0.1:8000/docs` for interactive API docs.

Visit `http://127.0.0.1:8000/` for the web UI — create tickets, filter by
status, view details, confirm categories, and update status, all from
the browser.

Or run the whole stack (app + Postgres) with Docker:

```bash
docker compose up --build
```

In a second terminal, use the CLI:

```bash
python cli.py create "The VPN is down and I cannot access anything"
python cli.py list
python cli.py status 1 in_progress
python cli.py confirm-category 1 connectivity
```

Configurable via environment variables (no hardcoded values) if the
server isn't on the default local address, or has `API_KEY` set:

```bash
export TICKETBASE_API_URL=https://tickets.example.com
export TICKETBASE_API_KEY=your-api-key-here    # only needed if the server has API_KEY set
export TICKETBASE_TIMEOUT=15                    # seconds, default 10
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

## Production readiness (added on top of Project 2)

The original SupportRAG build worked, but had real gaps for anything
beyond a local demo. These close them:

**Config (`app/config.py`)** — every tunable value (DB URL, API key,
RAG thresholds, rate limits, pagination size, log level) comes from
`pydantic-settings`, overridable via env vars or a `.env` file (see
`.env.example`). Nothing production-relevant is a hardcoded literal
buried in a random module anymore.

**Fixed a real performance bug in the embedder.** The original
`SupportRAGService` re-fit the TF-IDF/SVD embedder from scratch on
*every single request* - wasted CPU for data that never changes between
requests. Fixed by building a `RAGIndex` once, at app startup (FastAPI's
`lifespan` handler), caching it in memory, and having each request's
`SupportRAGService` just read from it. Verified with a timing test: 15
consecutive `/suggest` calls now run in ~2-5ms each after the one-time
~25ms startup build, instead of re-paying that cost every time.

**Optional API-key auth (`app/auth.py`)** — off by default (so the test
suite and local dev need no secret), on when `API_KEY` is set in the
environment. Gates the write endpoints (`POST`/`PATCH`); read endpoints
and the web UI stay open, since browser-session auth is a different,
out-of-scope concern (see Non-goals). Verified with dedicated tests
covering: rejected with no key, rejected with wrong key, accepted with
correct key, reads still open, and auth-off-by-default.

**Rate limiting** — `slowapi`, applied to `/suggest` specifically (the
most compute-heavy route), default 20/minute, configurable via
`SUGGEST_RATE_LIMIT`. Verified manually: with the limit set to
3/minute, the 4th call in the same minute correctly returns 429.

**Structured logging** — every request logs method/path/status/timing;
every SupportRAG suggestion logs its category, confidence, and which
article IDs it drew on (or that it abstained, and why). This is what
makes an "explainable" AI feature actually auditable after the fact,
not just explainable in theory. Unhandled exceptions get a global
handler that logs the full traceback server-side but returns a clean
JSON 500 to the client - no leaked stack traces.

**Alembic migrations** — replaced the old `Base.metadata.create_all()`
auto-create with a real, versioned migration
(`alembic/versions/..._initial_schema...py`), autogenerated from the
SQLAlchemy models and pointed at `DATABASE_URL` via the app's own
config (see `alembic/env.py`). Verified: `alembic upgrade head`,
`alembic downgrade base`, and `alembic upgrade head` again all run
cleanly, and the app works correctly against a migrated database.
`create_all()` is still called at startup too, as a dev-convenience
no-op once the schema's already been migrated - production deployments
should run migrations explicitly before starting the app (see the
Dockerfile).

**Full containerization** — `Dockerfile` for the app, `docker-compose.yml`
now runs the app *and* Postgres together (`docker compose up --build`),
with a Postgres healthcheck gating app startup and migrations running
automatically before the app boots. Honest caveat: this sandbox has no
Docker daemon, so I couldn't actually run `docker compose up` here - I
verified every piece it depends on independently (migrations apply
cleanly, `requirements.txt` installs cleanly, the app starts and serves
correctly), but the compose file itself is untested end-to-end. Worth
running yourself before calling it "deployed."

**CI (`.github/workflows/ci.yml`)** — runs the full test suite and
checks the Alembic migration applies cleanly, on every push/PR. I
simulated the exact steps locally (fresh dependency install, `pytest`,
`alembic upgrade head` against a clean DB) and confirmed they pass; the
workflow itself only runs once this repo is actually pushed to GitHub.

**API hygiene** — `GET /tickets` now takes `limit`/`offset` (capped by
`MAX_PAGE_SIZE`) instead of always returning everything; `/health` now
actually checks DB connectivity (`SELECT 1`) instead of unconditionally
returning `ok`.

**A fourth debugging story, and a real one - this shipped, then broke on
someone's machine.** The new `app/auth.py` used `str | None` (Python
3.10+ union syntax) for the `X-API-Key` header parameter. This project's
own git history had already fixed this exact class of bug once before
(see the Python 3.9 compatibility fix in the commit log) - and I
reintroduced it anyway in new code, because everything I tested ran on
this environment's Python 3.12, where that syntax works fine. It only
surfaced when run on a Mac with Python 3.9 (`str | None` raises
`TypeError: unsupported operand type(s) for |: 'type' and 'NoneType'`
at import time, not just at type-check time - FastAPI actually
evaluates parameter annotations at runtime to build the route). Fixed
by switching to `typing.Optional[str]`, consistent with how the rest of
the codebase already handles this. More importantly: added
`tests/test_python39_compat.py`, which parses every file in `app/` with
Python's own `ast` module and fails if it finds unguarded `X | None`
syntax in a type-annotation position - specifically scoped to
annotations only, not real bitwise-OR expressions, to avoid false
positives. I verified it actually works by deliberately reintroducing
the bug, confirming the test failed and pointed at the exact line, then
restoring the fix and confirming it passed again. This is what actually
prevents this bug from coming back, regardless of which Python version
happens to be running the test suite - a comment or a one-off manual
check wouldn't have.

## Optional LLM-drafted responses (free tier)

SupportRAG's drafted response can optionally be rewritten by a real
LLM instead of the plain template - see `app/llm.py`. This is off by
default (empty `LLM_API_KEY` = disabled, same pattern as `app/auth.py`)
and, when off, behaves identically to before - every existing test
still passes unmodified.

**Why Groq, and why it's genuinely free:** researched current (2026)
free-tier LLM APIs before picking one. Groq offers a free, no-credit-
card developer tier (30 requests/minute, 14,400/day) over an OpenAI-
compatible API, with several open-weight models available. `LLM_API_BASE`/`LLM_MODEL` are just config, so
switching to another OpenAI-compatible free provider (OpenRouter,
Cerebras) - or another model on Groq itself - needs no code change.

**This volatility isn't hypothetical - it happened during testing.**
The default model this section first shipped with
(`llama-3.3-70b-versatile`) stopped being available on a real account
within weeks: a live `curl https://api.groq.com/openai/v1/models` call
during testing showed it gone from the catalog entirely, replaced by
models like `openai/gpt-oss-20b` and `qwen/qwen3.8-27b`. The app
behaved exactly as designed when this happened - every suggestion
correctly fell back to the deterministic template with a clear
`WARNING ticketbase.llm` log line, rather than breaking - but the
default needed updating regardless, now `openai/gpt-oss-20b`. This is
the single best piece of evidence in this whole project that the
fail-safe design was worth building: it wasn't a hypothetical resilience
story, it's what actually happened on a real account during real testing.

**What the LLM is and isn't allowed to do.** The prompt in `app/llm.py`
is deliberately close-ended: rewrite this specific retrieved article
for this specific ticket, using only what's in the article, under 120
words, no invented facts. It never sees the category-selection step and
is never called when retrieval abstained - the LLM can only change
*wording*, never *what gets suggested or whether something gets
suggested at all*. That split (retrieval decides; the LLM, if present,
only rephrases) is what keeps the existing abstain/cite-sources
guarantees intact regardless of whether the LLM step is even enabled.

**Failure handling, actually tested against real failure, not just
mocked ones.** Free-tier LLM catalogs are volatile in practice - while
researching this, I found a documented case of a provider's free model
list collapsing from a dozen entries to two within months. So every
failure mode (timeout, non-200 status, malformed response, network
unreachable) falls back to the existing template rather than breaking
the suggestion endpoint. `tests/test_llm.py` covers this with mocked
network responses (success, timeout, 404, malformed JSON, and the
"never called on abstain" safety property) - and I additionally ran a
real, unmocked call from this sandbox (whose network genuinely can't
reach `api.groq.com`) and confirmed the exact same code path handles a
real HTTP 403 from the egress proxy correctly: logs why, falls back,
keeps the endpoint at 200. Both the mocked and the real-network
evidence point the same way.

**To actually try it:** get a free key at
[console.groq.com](https://console.groq.com), set `LLM_API_KEY` in
`.env`, restart the app. The UI's "Drafted response" disclosure will
say "(AI-written, grounded in the source above)" instead of
"(templated from the source above)" once it's working.

## Search, and AJAX suggestion loading

Two smaller additions, both motivated by real gaps rather than added
for their own sake:

**Search** — `GET /tickets?q=...` (and the web UI's search box) filters
by description substring, case-insensitive, composable with the
existing `status` filter. `crud.list_tickets` gained a `q` parameter;
covered by two new tests (`test_list_tickets_search_by_description`,
`test_list_tickets_search_combines_with_status_filter`).

**AJAX-loaded suggestions** — the LLM step above means `/suggest` can
now take a few seconds instead of milliseconds. A full-page form POST
with no feedback during that wait looks frozen, so `app/static/app.js`
intercepts the "Suggest a category" form, shows a pulsing loading state
while the request is in flight, and renders the result client-side from
the same JSON the API already returns - no new endpoint. This is
progressive enhancement, not a requirement: the plain HTML form POST
(`/ui/tickets/{id}/suggest`) still works completely unchanged if
JavaScript fails or is disabled, and the JS itself falls back to a
normal form submit if the `fetch()` call errors. Verified the JS
references the exact field names the API actually returns (a quick
Python script cross-checked `data.category`, `data.sources[].title`,
etc. against a real API response), and checked the file parses as
valid JavaScript with `node --check` - I don't have a real browser in
this sandbox to confirm the rendered DOM visually, so a quick look in
an actual browser is still worth doing yourself.

Also added the same rate limit to the no-JS fallback route
(`/ui/tickets/{id}/suggest`) that the JSON API route already had - it
runs the identical compute-heavy suggestion logic and had been missed.

## Related tickets ("has this happened before?")

Real ticketing tools leave this as manual work: an agent has to think
to search for a duplicate, then search well, then read through results
themselves. `GET /tickets/{id}/related` (and an automatic panel on
every ticket's detail page - no button, since unlike a category
suggestion there's no "confirm" step attached to it) surfaces up to 3
similar past tickets automatically.

**Deliberately reuses infrastructure, doesn't add new infrastructure.**
`app/related_tickets.py` reuses the exact same fitted TF-IDF/SVD
embedder that `RAGIndex` already builds for the knowledge base -
no second vocabulary to fit or keep in sync. The tradeoff is the same
one already documented for SupportRAG (TF-IDF matches shared
vocabulary, not deep meaning), and it's an honest one here too: I
tested it against three tickets (two genuinely about VPN issues, one
about billing) and it correctly matched the two VPN tickets at 97%
similarity while correctly excluding the unrelated one - verified from
the actual JSON response and the actual rendered HTML, not assumed.

**A real, named scaling limit, not a hidden one.** Unlike the knowledge
base (16 fixed articles, embedded once at startup), tickets are created
continuously, so there's no fixed corpus to pre-embed at startup. This
computes embeddings for candidate tickets on the fly, per request,
capped at the 300 most recent other tickets
(`crud.list_other_tickets`). That's fine at portfolio scale but would
need precomputed, stored embeddings (same pattern as
`KnowledgeArticle.embedding`) for a real high-volume deployment - noted
in Next steps below rather than silently left for someone to discover.

Covered by 6 tests (`tests/test_related_tickets.py`): finds a genuinely
similar ticket, never returns the ticket itself, returns an empty list
rather than a forced weak match when nothing is actually similar,
handles zero-other-tickets and nonexistent-ticket cases, and confirms
the panel actually renders in the HTML.

## Next steps

- Swap `DATABASE_URL` to the Postgres URL and run `docker-compose up -d`
  to develop against Postgres instead of SQLite
- Migrate `KnowledgeArticle.embedding` to a native `pgvector` column and
  push similarity search into the database
- Grow the knowledge base beyond 16 seed articles (ideally from real
  resolved tickets) to reduce the TF-IDF noise described above
- Store each ticket's embedding at creation time (same pattern as
  `KnowledgeArticle.embedding`) instead of recomputing it on every
  related-tickets lookup, once ticket volume goes beyond portfolio scale
- Optionally add a generative step (an LLM call) that rewrites the
  drafted response in a more natural voice, while keeping the same
  cited-sources-and-abstain-path structure - the retrieval layer this
  version built is what makes that safe to add later (this is now done -
  see the Optional LLM-drafted responses section above)
- Actually run `docker compose up --build` end-to-end somewhere with a
  Docker daemon, since that step couldn't be verified in this sandbox
