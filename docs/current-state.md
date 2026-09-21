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

---

## Milestone 1 (partial): agent identity, sessions, roles, CSRF

Built and verified in a second session. Covers the identity/access
control piece of Milestone 1. Explicitly NOT covered by this slice:
customer/contact records, ticket assignment, actor-attributed audit
events on tickets themselves, or optimistic concurrency - those remain
open (see below).

### What was built

- `User`, `Session`, `Invite` models (`app/models.py`). Sessions are
  opaque server-side tokens, not JWTs - deliberately, so a session can
  be instantly revoked by marking one row, which a self-contained
  signed token can't do without a separate revocation-list mechanism.
- Argon2id password hashing (`argon2-cffi`, a maintained library - see
  `app/security.py`). Verified passwords are never stored in plaintext
  (`test_password_is_never_stored_in_plaintext`).
- Three roles: admin, agent, reviewer (read-only). Enforced via
  `app/auth.py::require_role`.
- Invite-only account creation - there is no open signup route. An
  admin creates an invite (`POST /invite`); the invitee sets their own
  password via a one-use, expiring token (`POST /accept-invite`). No
  email-sending system exists yet (that's Milestone 2), so the invite
  link is shown directly to the admin rather than emailed - a real,
  documented simplification, not a security gap (the token itself is
  still random, unguessable, expiring, and one-use).
- CSRF protection (`app/auth.py::require_csrf`) on every cookie-
  authenticated state change, using a synchronizer token derived via
  HMAC from the session ID and a server secret - nothing extra to
  store or look up, and a token is automatically invalid the moment
  its session is.
- Browser UI routes now require a real logged-in session
  (`require_agent`) instead of the Milestone-0 stopgap (`require_api_key`,
  which browsers can't practically send anyway). Reads require any
  role; writes require admin or agent (reviewer is genuinely read-only,
  enforced server-side, not just hidden in the UI).
- A bootstrap-admin mechanism (`BOOTSTRAP_ADMIN_EMAIL`/`_PASSWORD` in
  `.env`) creates exactly one admin account at startup, only when the
  users table is completely empty - solves the chicken-and-egg problem
  of invite-only creation needing a first inviter.
- Login is rate-limited (`LOGIN_RATE_LIMIT`, default 10/minute).
- A real Alembic migration (`7420c85d3ce8`) for the three new tables.

### Verified against a REAL PostgreSQL 16 server, not just SQLite

A real PostgreSQL 16 server was installed and run in the build sandbox
specifically to test this properly (`apt-get install postgresql`,
started via `service postgresql start`), since the brief is explicit
that Postgres should be authoritative for this kind of concurrent,
session-based system, and SQLite is more forgiving in ways that can
hide real bugs.

Real commands run, real results:

```
$ alembic upgrade head   # against postgresql://ticketbase:ticketbase@localhost:5432/ticketbase
Running upgrade  -> 9407dd1d2182, initial schema: tickets and knowledge_articles
Running upgrade 9407dd1d2182 -> 7420c85d3ce8, add users, sessions, invites for agent auth
```

A scripted end-to-end run against that real database confirmed, in order:
unauthenticated `GET /` redirects to `/login`; login with correct
bootstrap-admin credentials succeeds and sets a session cookie; wrong
password is rejected (401); an authenticated request can view the
homepage; a ticket-creation POST with no CSRF token is rejected (422);
an admin can create an invite and the token can be extracted from the
real rendered response; a completely separate, previously-unauthenticated
test client can accept that invite and gets its own session; the new
account can independently log in with its own credentials; logging out
revokes the session such that the *same* old cookie is rejected
afterward (not just cleared client-side - the server-side row is
actually marked revoked). A second run confirmed a CSRF token that is
present but wrong is rejected (403, distinct from the missing-token
422), and that a reviewer-role account can read the ticket list (200)
but is blocked from creating a ticket (403).

### Automated test suite

```
$ python -m pytest -q
60 passed, 3 warnings
```

17 of those are new, in `tests/test_agent_auth.py`, translating the
manually-verified real-Postgres flow above into permanent pytest
coverage (still running against the SQLite test database - see "Known
limitation" below). Covers: login success/failure/unknown-email (with
a deliberately generic error message to avoid email enumeration),
password hashing, session revocation on logout, CSRF missing/wrong/
correct, reviewer read-only enforcement, agent write permission, admin-
only invite access, full invite-and-accept flow, double-use invite
rejection, invalid token rejection, and short-password rejection.

A debugging note worth keeping, in the same spirit as this project's
earlier ones: running the new auth test file in full (not in isolation)
initially produced spurious failures that looked like broken login.
The real cause was the login route's own rate limiter - a genuine
security feature - firing because slowapi's in-memory rate-limit
storage persists across the whole pytest process, not per test, so many
tests each logging in via their own fixture eventually tripped the same
real 10/minute limit. Fixed by resetting `app.state.limiter` in the
shared per-test teardown fixture. Two other apparent failures were pure
test-writing mistakes (forgetting `follow_redirects=False`, so a
correct 303 redirect was silently followed to a 200 by the test client)
- caught and fixed once the actual response codes were inspected rather
than assumed.

### Known limitations, stated plainly

- **The automated pytest suite still runs on SQLite**, not the real
  Postgres server set up this session. The Postgres verification above
  was done with hand-written scripts, not wired into `pytest` or CI.
  Making `tests/conftest.py`'s database configurable (and adding a
  Postgres service to CI) is real, valuable remaining work - explicitly
  not done here.
- **No customer/contact model yet.** "Related tickets" remains
  similarity-based, not identity-based - exactly as flagged in the
  original brief review above.
- **No ticket-level audit trail.** `crud.confirm_category` still just
  flips a boolean; there is no record of *which* agent confirmed it.
  Adding an actor-attributed event log is real remaining Milestone 1
  scope, not done in this slice.
- **No optimistic concurrency** on ticket edits.
- **`SECRET_KEY` auto-generation is a dev convenience, not a production
  answer.** If unset, a random key is generated per-process and logged
  as a warning - sessions/CSRF break across a restart, and multiple
  processes would each get a different key. A real deployment needs a
  real, stable `SECRET_KEY`.
- **Docker/Compose were not updated** to forward the new auth-related
  environment variables (`SECRET_KEY`, `BOOTSTRAP_ADMIN_*`, etc.) - the
  Docker gaps flagged in the Milestone 0 section above are still open,
  now with a slightly longer list of variables that would need forwarding.
- The AJAX suggestion flow (`app/static/app.js`) calls the JSON API's
  `/tickets/{id}/suggest` directly, which remains ungated by the new
  session system by design (it was already open before this milestone,
  matching the JSON API's existing optional-API-key posture) - not a
  new regression, but worth naming so it isn't assumed to be covered.

### Next milestone

Either continue Milestone 1 (customer/contact records, ticket
assignment, actor-attributed audit trail, optimistic concurrency) or
move to wiring Postgres into the automated test suite and CI first,
since that's now genuinely possible (a real Postgres server has been
proven to work) and would strengthen every future milestone's testing,
not just this one.

---

## Milestone 1 (complete): customers, assignment, audit trail, concurrency

Built and verified in a third session, finishing what the two sections
above left open. This closes Milestone 1's acceptance gate: "one
returning-customer workflow works without AI."

### What was built

- `Customer` and `Contact` models. Customers are companies; contacts are
  people at them. Phone numbers are normalized for lookup
  (`crud.normalize_phone`) - strips formatting and a US/Canada country
  code, so "+1 (555) 123-4567" and "555-123-4567" match as the same
  number. Explicitly NOT full E.164 handling (documented in the
  function's own docstring) - a real phone integration (Milestone 4)
  would use a proper library.
- `Ticket` gained `customer_id` and `assignee_id` (both nullable, so
  existing tickets aren't broken) and `version` (for optimistic
  concurrency, below).
- **Exact customer history, kept structurally separate from semantic
  "related tickets".** `crud.get_customer_tickets` filters by the real
  `customer_id` foreign key. Verified with a test that specifically
  checks a similar-sounding ticket from a DIFFERENT customer never
  appears in another customer's history
  (`test_exact_customer_history_never_shows_another_customers_ticket`) -
  this was the brief's specific concern about conflating the two.
- **An actual audit trail.** `AuditEvent` records actor, action,
  resource, and timestamp. `crud.confirm_category`, `update_status`,
  `assign_ticket`, and `link_ticket_to_customer` all write one, in the
  SAME transaction as the change itself (the audit row and the change
  are added to the session together, then one `db.commit()`) - so they
  can't end up partially applied. Directly closes the brief's finding
  that a boolean can't prove a human acted: the ticket page now shows
  "Test Admin — category confirmed — Sep 21, 06:34 UTC", not just a
  category with no history.
- **`actor_user_id` is nullable, on purpose.** A write made via the
  shared JSON-API key has no real per-human identity behind it - a
  shared secret can't prove which person acted. Rather than fabricate
  an actor, those audit events honestly record `actor_user_id=None`.
  Verified with `test_json_api_write_creates_audit_event_with_no_actor`.
- **Optimistic concurrency.** Every ticket write (JSON API and UI) now
  requires the `version` the caller last read. A write against a stale
  version is rejected - `409` for the JSON API, a re-rendered ticket
  page with a clear explanation for the UI (`crud.VersionConflict`) -
  instead of silently overwriting a concurrent change. Verified for
  both surfaces: `test_json_api_stale_write_rejected_with_409` and
  `test_ui_stale_write_rejected_and_shows_current_state`.
- Assignment (`/ui/tickets/{id}/assign`) and customer linking
  (`/ui/tickets/{id}/customer`), both agent/admin only, both
  version-checked and audit-logged like every other write.
- New pages: `/customers` (search + create), `/customers/{id}` (exact
  history + contacts), wired into the ticket detail page.

### Two real bugs found while building this, fixed before claiming done

1. **The auto-generated Alembic migration would have failed on any
   database that already had ticket rows.** `version` was generated as
   `NOT NULL` with no default - both SQLite and Postgres reject that
   ALTER TABLE outright against a non-empty table. Fixed with
   `server_default='1'`, matching the model's Python-side default.
   Caught by deliberately testing the realistic case, not just a fresh
   empty database: inserted a real ticket under the OLD schema, then
   ran the new migration on top of it, on both SQLite and a real
   Postgres server, and confirmed the existing row survived with
   `version=1`, `customer_id`/`assignee_id` both `NULL`.
2. **SQLite has no `ALTER TABLE ADD CONSTRAINT`.** The autogenerated
   migration's `op.create_foreign_key` calls raised
   `NotImplementedError` outright on SQLite - confirmed by actually
   running it and seeing the error, not by inspection. Fixed by
   wrapping the `tickets` table changes in `batch_alter_table`, which
   does a copy-and-recreate on SQLite and is a thin equivalent wrapper
   on Postgres - the same migration file was then verified to work on
   both.

### Two real bugs found while writing tests, same discipline as before

1. **`assignee_id: str = Form(...)` couldn't represent "unassign".**
   FastAPI/Starlette treats an empty-string value for a *required* form
   field as MISSING, not present-but-empty - confirmed directly (a
   422 "Field required" with `"input": null`, not the empty string
   sent). The right fix was semantic, not a workaround: made the field
   `Optional[str] = Form(default=None)`, since "absent means unassign"
   is what the field actually means.
2. **Phone normalization was too strict to be useful.** The first
   version didn't strip a leading `+1`/`1` country code, so "+1 (555)
   123-4567" and "555-123-4567" - two realistic ways of entering the
   SAME number - normalized to different values and failed to match.
   Fixed, and the fix is specifically tested
   (`test_phone_normalization_matches_differently_formatted_numbers`),
   not just asserted to work.

### Verified against real PostgreSQL again

Beyond the migration testing above: created a customer, linked a
ticket to it, confirmed its category, and deliberately attempted a
stale write - all against a real PostgreSQL 16 database, not SQLite.
The stale write was correctly rejected with 409, and the audit trail
correctly showed the real admin's name next to "category confirmed."

### Automated test suite

```
$ python -m pytest -q
75 passed, 3 warnings
```

15 new tests in `tests/test_milestone1_features.py` (customers,
contacts, phone normalization, exact-history isolation, assignment,
audit trail with and without a real actor, optimistic concurrency on
both the JSON API and the UI).

### Known limitations, stated plainly

- No customer portal or self-service - customers are managed entirely
  by agents/admins through the UI built here.
- No caller-ID/phone-based lookup UI yet - `crud.find_contacts_by_phone`
  exists and is tested, but nothing in the UI calls it yet (that's
  Milestone 4's phone integration).
- Contact search is by customer only (`list_contacts_for_customer`) -
  there's no global "search all contacts" page yet.
- The automated pytest suite still runs on SQLite; the Postgres
  verification above (both for the migration and for this session's
  functional smoke test) was done with hand-written scripts, not wired
  into CI. Still an open, named item from the previous section.
- No email notifications on assignment, no @mentions, nothing
  Milestone-2-shaped - deliberately out of scope for this slice.

### Next milestone

Milestone 1's acceptance gate is now met: a returning-customer workflow
(create customer → link ticket → confirm category → see it in that
customer's exact history, with a real audited actor throughout) works
without any AI or telephony dependency. Reasonable next steps, in
rough priority order: wire Postgres into the automated test suite/CI
(now proven to work, not yet automated); Milestone 2 (reviewed email
drafts with a real approval + delivery workflow); or CLI auth/config
improvements (still an open item from the original brief review).
