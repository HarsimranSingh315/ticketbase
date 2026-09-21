# Current state — TicketBase

Written in response to `TicketBase-Production-Brief-and-Claude-Prompt.md`.
This document follows that brief's own instruction: verify claims against
actual code and report real commands and real results, not intentions.

## What this document is

A snapshot of what's actually true about the codebase right now: what's
implemented, what's tested, what was verified in this session with a real
command and a real result, and what remains open. Anything not explicitly
marked "verified" below should be treated as unverified.

## Baseline check

```
$ python -m pytest -q
43 passed, 3 warnings
```

Warnings: FastAPI/Starlette TestClient deprecation notices, and one
SQLAlchemy identity-map warning specific to a test that manually
manipulates a session already used elsewhere in the same test (see
`tests/test_supportrag.py::test_kb_drift_triggers_automatic_reseed`) -
not present in normal application operation, where each request gets
its own fresh session.

## Findings from the brief: verified status

| Finding | Status | Evidence |
| --- | --- | --- |
| Shared API key doesn't protect browser writes | **Fixed this session** | Reproduced before fixing: JSON `PATCH /tickets/{id}/category` without a key returned 401; the browser route `POST /ui/tickets/{id}/category` returned 200 and **persisted** `category_confirmed=True` with zero authentication. Fixed by gating `POST /ui/tickets`, `POST /ui/tickets/{id}/status`, and `POST /ui/tickets/{id}/category` behind the same `require_api_key` check as their JSON equivalents. Regression tests added: `test_ui_category_write_rejected_without_api_key`, `test_ui_ticket_creation_rejected_without_api_key`, `test_ui_status_update_rejected_without_api_key` (all passing). |
| Ticket reads not protected by the same policy | **Known limitation, not fixed this session** | Confirmed: `GET /tickets/{id}` returns 200 with no key even when `API_KEY` is set. Deliberately NOT gated this session - doing so would lock out the entire browser UI with no working alternative, since no real login/session system exists yet. This is exactly what Milestone 1 (real agent auth) is for. Documented here rather than silently left, per the brief's own standard. |
| `crud.confirm_category` sets state with no authenticated actor or approval record | **Not fixed - Milestone 1 scope** | Confirmed by reading `app/crud.py` and `app/models.py`: `category_confirmed` is a boolean with no actor, timestamp, or version attached. This requires the identity/session model from Milestone 1 before it can be meaningfully fixed - adding an "actor" field with no real authenticated users behind it would be cosmetic, not a real fix. |
| AJAX loading state destroys the form the fallback depends on | **Fixed this session** | Confirmed by code review: the old `renderLoading()` replaced `#suggestion-container`'s `innerHTML`, which contained `#suggest-form` as a child - destroying it. The failure path then called `.submit()` on a detached form, and there was no visible error state at all on failure. Rewritten (`app/static/app.js`) to never touch the form until a final result exists: only the button's disabled/text state changes during the request, and failures show a specific, visible inline error message (rate-limited vs server error vs network failure get different text) with the form still intact and resubmittable. Not verified in a real browser (no browser available in this environment) - verified by static review and that the file parses as valid JS (`node --check`), and that the JSON contract it depends on matches the real API response shape. **A real-browser check of this is still owed.** |
| `app/llm.py` empty/null content handling | **Partially addressed in an earlier session, gap remains** | An empty-`content` response (a reasoning model spending its token budget on reasoning) is now handled and logged - this was found and fixed via real testing on a live Groq account earlier in this project's history. NOT yet addressed: the brief's broader point that other malformed shapes could still raise, and that `reasoning_effort` is sent unconditionally regardless of whether the target model/provider supports it. Open. |
| KB drift detection is title-only; content changes with the same title are missed | **Confirmed, not fixed this session** | `app/supportrag.py::seed_knowledge_base` compares title sets only. Editing an existing article's content without renaming it will NOT trigger a reseed. The existing drift detection (added earlier this session, before this brief) does correctly handle *added/removed* articles - confirmed via `test_kb_drift_triggers_automatic_reseed`. Content-level versioning (stable IDs + content hash) is not implemented. Open. |
| `related_tickets.py` mixes semantic similarity with "customer history" | **Confirmed, architecturally accurate as described, not addressed** | Correct as described: there is no customer/contact model at all yet (Milestone 1), so "related tickets" is genuinely the only history mechanism that exists, and it is similarity-based, not identity-based. The brief's point that these must be kept separate is a Milestone 1+ concern once real customer records exist. |
| Similarity shown as a percentage reads as confidence | **Confirmed, not fixed this session** | Accurate: `app/templates/ticket_detail.html` and `app/static/app.js` both render `similarity` as "XX% similar," and the UI section is literally labeled "confidence" in places. This is a real, fixable labeling issue independent of the bigger milestones - flagged here, not yet fixed. |
| Docker: root container, no `.dockerignore`, migrations run per-container-start | **Confirmed, not fixed this session** | `Dockerfile` runs as root (no `USER` directive), no `.dockerignore` file exists, and `CMD` runs `alembic upgrade head` before every container start rather than as a separate one-time release step. Open - Milestone 0/5 scope. |
| CI migration check uses SQLite, not PostgreSQL | **Confirmed, not fixed this session** | `.github/workflows/ci.yml`'s migration check runs against a SQLite file. The brief is correct that this doesn't validate PostgreSQL-specific migration behavior, which matters since PostgreSQL is the target production database. Open. |
| `tests/conftest.py` overrides the real startup lifecycle | **Confirmed, accurate description, not changed this session** | Correct: tests never run the app's real `lifespan` handler (see `conftest.py`'s own docstring, written earlier this session, which explains why). This is a deliberate tradeoff for test isolation/speed, but it does mean the real startup path (including the KB drift-detection code fixed earlier) is not exercised by the automated suite the way a real `uvicorn` process exercises it - only by manual scripted verification. A real-lifespan integration test is legitimate to add and is not yet present. |
| `cli.py` lacks configurable API URL, auth headers, timeouts | **Confirmed, not fixed this session** | `cli.py`'s `API_BASE` is a hardcoded constant; there is no way to pass an API key or configure a timeout. Open. |

## What changed this session (Milestone 0 partial)

1. **Fixed**: browser UI write routes now require the same `X-API-Key` auth as
   the JSON API when auth is enabled, closing a real, verified security gap.
   3 new regression tests.
2. **Fixed**: the AJAX suggestion-loading state no longer destroys the form
   it may need to fall back to, and failures now show a real, specific,
   visible error message instead of silently hanging. Not yet verified in
   an actual browser.
3. Added this document, per the brief's required workflow step 1.

## Explicitly NOT done in this session (do not assume otherwise)

- No real agent authentication (login, sessions, password hashing, CSRF).
  `API_KEY` remains a single shared secret, appropriate only for a
  single-operator local/demo setup, not multiple real users.
- No customer/contact data model.
- No approval/audit trail with an actual authenticated actor.
- No email outbox, worker process, or Resend integration.
- No phone/Twilio integration.
- No deployment. Nothing has been provisioned on Render, AWS, or any
  cloud provider. The app runs only locally.
- No PostgreSQL-specific testing was performed (SQLite only, in this
  environment).
- No load, security scan, or penetration testing was performed.
- Docker/`.dockerignore`/non-root container hardening not done.

## Next milestone

Milestone 1 (Secure case management) per the brief: agent identity,
sessions, role/assignment-based authorization, a customer/contact model,
searchable exact customer history (separate from semantic "related
tickets"), assignment, actor-attributed audit events, and validated
status transitions with optimistic concurrency - all demonstrable via one
working manual support workflow with no AI or telephony dependency.

This is a substantial, multi-session undertaking on its own and has not
been started beyond the auth-related Milestone-0 fixes documented above.
