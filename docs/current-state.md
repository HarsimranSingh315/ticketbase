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

---

## Closing the tracked backlog (before starting Milestone 2)

Three items had been explicitly flagged as open across the sections
above and kept getting deferred. Closed all three in one session,
each with real verification, before moving forward.

### 1. Postgres wired into automated CI (previously: manual scripts only)

`tests/conftest.py`'s database is now configurable via
`TEST_DATABASE_URL` (defaults to SQLite in-memory - zero setup for
local dev). `.github/workflows/ci.yml` now runs the *entire* 84-test
suite twice per push: once against SQLite, once against a real
Postgres 16 service container that GitHub Actions runs and
health-checks automatically. It also runs the Alembic migration check
against Postgres twice - once from empty, and once specifically
reproducing the upgrade-with-existing-data scenario that this
project's own migration broke on before (see the Milestone 1 section
above) - a real regression check, not a hypothetical one.

I can't run GitHub Actions itself from this sandbox, so I verified
this the same way as everything else in this project: by running every
individual command the workflow file contains, by hand, against a real
local Postgres server, and confirming each one before writing the YAML
- including the full 84-test suite passing against Postgres via
`TEST_DATABASE_URL=postgresql://... pytest`, and the schema-reset →
migrate → insert-a-ticket → migrate-to-head → verify sequence for the
upgrade-with-data check.

### 2. CLI: configurable + auth + timeouts (previously: hardcoded, and quietly broken)

`cli.py` now reads `TICKETBASE_API_URL`, `TICKETBASE_API_KEY`, and
`TICKETBASE_TIMEOUT` from the environment instead of a hardcoded
`API_BASE` constant with no auth support at all. 401s, connection
failures, and timeouts now produce a clear one-line message instead of
a raw `requests` traceback.

While fixing this, found that **the CLI was already broken against the
current API**: `status` and `confirm-category` never sent the
`version` field that ticket writes have required since Milestone 1's
optimistic-concurrency work - every real call would have 422'd. Fixed
by having both commands fetch the ticket first to get its current
version, which also means a genuine version conflict now surfaces as a
clear "run the command again" message instead of a generic HTTP error.
Verified against a real running server, with `API_KEY` actually
enabled: confirmed a missing/wrong key fails with a clear message, a
correct key works, `status` and `confirm-category` both succeed (proving
the version fix), and an unreachable URL fails cleanly rather than
throwing a stack trace. 9 new tests (mocked, so they don't need a live
server for every future test run) cover the same ground permanently.

### 3. Docker: non-root, `.dockerignore`, and migrations as a real release step

Three gaps, all fixed together since they touch the same two files:

- **No `.dockerignore` existed at all.** Added one - notably excluding
  `.env`, so a real secret can never end up baked into an image layer.
- **The app container ran as root.** Fixed with a dedicated `appuser`
  in the Dockerfile.
- **Migrations ran on every container start** (`alembic upgrade head &&
  uvicorn ...` as the app's own `CMD`) rather than once per release -
  a real risk with multiple replicas starting concurrently, not just a
  style preference. Split into a separate one-shot `migrate` service in
  `docker-compose.yml` that the `app` service waits on
  (`depends_on: migrate: condition: service_completed_successfully`)
  before starting.
- Runtime config (the growing list of `SECRET_KEY`, `LLM_API_KEY`,
  `BOOTSTRAP_ADMIN_*`, etc.) is now forwarded via `env_file: .env` on
  both the `migrate` and `app` services, instead of needing each new
  setting manually added to `docker-compose.yml` by name as the config
  surface grows - `.env` is read locally by compose at container-start
  time, never copied into the image.

Honest limitation, unchanged from every earlier mention of Docker in
this document: **this sandbox still has no Docker daemon**, so
`docker compose up --build` itself remains unrun here. What was
verified: the compose YAML parses correctly and its service dependency
graph is structurally sound (checked with a real YAML parser), and the
`alembic upgrade head` command the `migrate` service runs has itself
been run directly many times this project, against both SQLite and
real Postgres. The actual `docker compose up --build` run is still
worth doing yourself before calling this deployed - flagged the same
way, not silently assumed fixed.

### Test suite

```
$ python -m pytest -q
84 passed, 3 warnings
```

### Next milestone

The explicitly-tracked backlog is now closed. Milestone 2 (reviewed
email drafts: persisted drafts, an explicit approval step, a
transactional outbox, a durable worker, a local mail sink by default
and a real provider adapter when configured) is the next substantial
piece of new product surface, per the brief's own milestone ordering.

---

## Milestone 2: reviewed email drafts, approval, transactional outbox, worker

### What was built

- `OutboundMessage`: a drafted email tied to a ticket. `draft` →
  `approved` → `sent` (or terminal `failed`/`bounced`). Approval
  snapshots the exact recipient/subject/body into separate
  `approved_*` columns - the live fields stay editable only while still
  a draft; `crud.update_draft` and the approve route both refuse to
  touch an already-approved message (`crud.MessageNotDraft`).
- `OutboxJob`: the transactional outbox. `crud.approve_message` creates
  the message's approval snapshot AND its outbox job in the exact same
  `db.commit()` - there is no window where a message is approved but
  never queued, or queued without ever being approved. `operation_key`
  is derived deterministically from the message ID
  (`message-{id}`), not a fresh UUID per attempt, and is column-level
  UNIQUE - structurally impossible for two jobs to exist for one message.
- `worker.py`: a genuinely separate process (not a background task
  inside the web app - see its own docstring for why that separation
  matters). Claims jobs via `crud.claim_next_job`, a compare-and-swap
  `UPDATE ... WHERE status=<expected>` that's portable across SQLite
  and Postgres, with lease-based crash recovery (a worker that dies
  mid-job leaves it claimed until the lease expires, then another
  worker can pick it up). Bounded retries with exponential backoff;
  after `max_attempts` a job becomes terminally `failed`, not retried
  forever.
- `app/mail.py`: two adapters behind one interface. `LocalSinkAdapter`
  (default, zero config) durably records "sent" mail to a
  `local_sink_emails` table instead of any real network call -
  genuinely idempotent (a second `send()` with the same key is
  detected and treated as already-done, not duplicated - this is what
  makes a worker's retry-after-a-maybe-successful-send safe).
  `ResendAdapter` is a real integration, used only when
  `RESEND_API_KEY` is set.

### Resend: now live-verified, not just unit-tested

`ResendAdapter` was originally written and unit-tested (mocked) but not
verified against a live account - the same honest gap the Groq
integration once had. That gap is now closed: the user obtained a real
free Resend API key, configured it, drafted and approved a real message
through the running app, and ran `worker.py` as a genuinely separate
process against it. The worker's log line -
`sent message 1 (job 1) to <redacted>@gmail.com,
provider_id=01a0c5d7-909d-717d-a4b4-0ba92e7862f5` - shows a real
Resend-issued email ID (not the local sink's `local-N` placeholder
format), and the email was confirmed to have actually arrived in the
recipient's real inbox. This is the strongest evidence category this
project uses: a live account, a real send, and a human confirming
delivery, not just an API returning 200.

One real config detail this surfaced: Resend's free tier only allows
sending from the default `onboarding@resend.dev` address and only to
the exact email address the account signed up with, until a domain is
verified - `MAIL_FROM_ADDRESS` needs to be set to
`onboarding@resend.dev` accordingly for free-tier testing (documented
in `.env.example`).

Still not built: real webhook signature validation for delivery/bounce
callbacks - the local sink and this live-tested happy path both treat
"sent" as terminal, which is honest for what's actually implemented,
but a live provider's async delivered/bounced callbacks remain a real,
named gap.

### Two real bugs found via actual testing, fixed before shipping

1. **The migration broke on downgrade-then-upgrade, on Postgres only.**
   `sa.Enum(...)` creates a separate named Postgres TYPE alongside the
   table it's used in - `DROP TABLE` does not drop that type. So
   `alembic downgrade` followed by `alembic upgrade head` again failed
   with "type already exists" on the `CREATE TYPE` statement. SQLite
   has no such concept, so this was completely invisible there -
   exactly the class of bug the whole "test migrations against real
   Postgres" effort exists to catch, and it did. Fixed by explicitly
   dropping the enum types in `downgrade()`.

   Caught a second mistake while fixing the first: my initial fix
   claimed in a comment that running `DROP TYPE` unconditionally was
   safe because "SQLite silently no-ops it." I tested that claim
   directly instead of trusting it - `sqlite3.OperationalError: near
   "TYPE": syntax error`. It does not no-op; it raises. Fixed properly
   by guarding the statement to run only when `bind.dialect.name ==
   "postgresql"`.

2. **Test-writing mistakes, same two classes as earlier in this
   project, caught by actually running the tests rather than assuming
   they'd pass:** `worker.run_one_cycle()` opens its own `SessionLocal()`
   internally (correct for a real standalone process, but it means a
   test calling it directly hits the real dev/prod database unless
   patched) - fixed with `monkeypatch.setattr(worker_module,
   "SessionLocal", TestSessionLocal)`. And `TestClient` auto-follows
   redirects by default, so an assertion expecting `303` silently saw
   `200` instead until `follow_redirects=False` was added - the same
   mistake this project's auth tests hit earlier, still worth
   re-catching each time rather than assumed fixed once.

### Verified concurrency, not just claimed

`crud.claim_next_job`'s own docstring makes a specific claim: two
workers racing for the same job produce exactly one winner. This was
verified twice, at two different strengths:

- **In the pytest suite** (`test_concurrent_workers_racing_for_the_same_job_only_one_wins`),
  using two real, separate DB sessions - but run sequentially
  (`t1.join()` before `t2.start()`), noted honestly in the test's own
  comment, because SQLite's `StaticPool` serializes access to its
  single connection and can't exercise genuine concurrent access.
- **Separately, by hand, against real Postgres**, using a
  `threading.Barrier` to force two threads onto two separate
  Postgres connections at the same instant - actual concurrent access,
  not sequential-but-same-codepath. Result: exactly one winner, the
  loser correctly got `None`, and the job's `attempts` counter was `1`,
  not `2` (proving the loser's attempted UPDATE genuinely matched zero
  rows rather than double-incrementing). This is the strength of
  evidence the brief's own acceptance test asks for
  ("double-clicking send and concurrent workers create one send
  intent").

### Full manual end-to-end verification

Beyond the automated tests: ran the complete real flow by hand -
create draft → edit draft (version bumped correctly) → approve
(content snapshotted, real actor recorded) → attempt to edit the
now-approved message (correctly rejected, 409, content unchanged) →
run `worker.py --once` **as a genuine separate subprocess** (not a
function call inside the test process) → confirmed the message and
job both flipped to `sent`, and the local sink table captured the
exact approved recipient/subject/body.

### docker-compose.yml: a real worker service

Added a `worker` service alongside `postgres`/`migrate`/`app`, running
`python worker.py` continuously, waiting on the same
migrate-completes-successfully gate as `app`. Same honest limitation
as the rest of Docker in this project: no daemon in this sandbox to
actually run `docker compose up` with, so this is verified by
structure and YAML validity, not a live run.

### Test suite

```
$ python -m pytest -q
97 passed, 3 warnings
```

13 new tests in `tests/test_outbox.py`, plus the full suite re-verified
against real Postgres (`TEST_DATABASE_URL=postgresql://...`) - all 97
pass there too.

### Known limitations, stated plainly

- `ResendAdapter` was unverified against a live account when this
  section was first written - now live-verified (see the update above,
  added in a later session).
- No webhook/callback handling for real provider delivery, bounce, or
  failure events - the local sink's "sent is terminal" model doesn't
  need this, but a real deployment with Resend configured would need
  it built before delivery/bounce states mean anything beyond initial
  send.
- No UI surface for `bounced` state specifically (the template handles
  it, but nothing can currently set it without webhook handling).
- Editing a draft after creating it doesn't re-run SupportRAG or
  otherwise assist the agent - drafting a reply is entirely manual
  text entry right now, with no AI assistance in this slice (a
  reasonable, deliberately separate future enhancement, not attempted
  here).
- `docker-compose.yml`'s new `worker` service, like the rest of Docker
  in this project, is unverified end-to-end (no daemon available).

### Next milestone

Per the brief's ordering: Milestone 3 (a real customer-facing phone
integration) or closing the Resend/webhook gap named above with a real
provider account, whichever matters more for the actual next use case.

---

## Milestone 3: telephony (Twilio webhooks, caller-ID lookup)

### What was built

- `Call` model: `twilio_call_sid` is the natural idempotency key
  (UNIQUE) - Twilio sends multiple webhooks per call (initial ringing,
  then status changes, then a final callback with duration/recording),
  all carrying the same CallSid, and guarantees at-least-once delivery
  (retries happen). `crud.upsert_call_from_webhook` is the one function
  responsible for turning that into "one row per call, updated in
  place" rather than growing a new row per callback.
- `/webhooks/twilio/voice` (inbound call) and `/webhooks/twilio/status`
  (status/outcome callbacks) - both idempotent by CallSid, both attempt
  caller-ID lookup via the phone-normalization work already built in
  Milestone 1 (`crud.find_contacts_by_phone`), and both link a call to
  a contact ONLY on an unambiguous single match - a shared office line
  matching two different contacts, or no match at all, leaves the call
  unlinked for an agent to resolve rather than guessing.
- A **third distinct auth mechanism**, alongside the JSON API's shared
  key and the browser UI's sessions: these routes are called directly
  by Twilio's servers, so neither applies. Twilio's request signature
  is the entire security boundary - every route validates it FIRST,
  before touching any request data, and fails closed (rejects) on a
  missing/wrong signature or an unconfigured `TWILIO_AUTH_TOKEN`.
- UI: a call log (`/calls`) showing every call, matched or not, and a
  "Call history" section on each customer's detail page (the phone
  equivalent of `get_customer_tickets`'s exact ticket history).

### A real verification story worth keeping: don't trust a memorized value

Before writing any tests, I wrote a manual HMAC-SHA1 implementation of
Twilio's signature algorithm and tried to check it against a specific
test vector I recalled from training data. The two didn't match. Rather
than assume my code was wrong and start "fixing" a working
implementation, I installed the official `twilio` SDK and compared its
own `compute_signature()` output against mine, on the same inputs: they
were byte-for-byte identical. The memorized "expected" value was simply
wrong - not the algorithm.

Having confirmed my implementation was correct, I switched to the
official SDK anyway, for a reason unrelated to correctness: Twilio's own
webhook-security docs explicitly warn that hand-rolled validation has
real, documented failure modes their library tracks and a
point-in-time reimplementation wouldn't - empty-value form parameters
being silently dropped by some parsers (but included in Twilio's
signature), and "evolving parameter sets" Twilio adds without notice.
This is the same reasoning already applied to password hashing
(`argon2-cffi` over hand-rolled hashing in `app/security.py`): trust a
maintained library for a vendor-specific, evolving security contract,
even when a first-principles implementation is verified correct today.

Every "valid signature" test in `tests/test_telephony.py` computes a
**genuine** signature via the same `RequestValidator` the app uses, on
real (if synthetic) call data - this is real positive-path coverage of
the security boundary, not a mocked bypass. Also tested: a tampered
payload under an otherwise-valid signature (must fail - proves the
check binds to the actual params, not just the header's presence), and
the fails-closed behavior when no auth token is configured at all.

### The Postgres enum-type migration bug, caught proactively this time

Milestone 2's migration broke on Postgres specifically because
`DROP TABLE` doesn't remove the separate named ENUM TYPE that
`sa.Enum(...)` creates alongside it (see that section above for the
full story). Rather than rediscover this the same way, the fix was
applied to this migration's `downgrade()` before ever testing it - and
then the full upgrade → downgrade → upgrade round trip was verified on
both SQLite and real Postgres anyway, to confirm the proactive fix
actually worked rather than assuming it did.

### A subtle enum encoding question, checked rather than assumed

`CallStatus.in_progress = "in-progress"` and `CallStatus.no_answer =
"no-answer"` are the first enums in this project where the Python
member NAME (which can't contain a hyphen) differs from its VALUE
(which matches Twilio's own hyphenated vocabulary, e.g. `CallStatus`
in Twilio's API). Every earlier enum in this codebase had name==value,
so this was untested territory: does SQLAlchemy store the member name
or the value in the database, and does that match what a real Postgres
native ENUM type (created with the member *names* as its labels)
actually accepts? Checked directly rather than assumed - inspected the
raw database column value (not just Python's read-back) on both
SQLite and real Postgres, including the double-hyphen `no-answer`
case specifically. Confirmed correct on both: SQLAlchemy resolves the
Twilio-format value string to the correct member before writing, and
the stored raw value is the underscored name, matching the Postgres
type definition.

### Full manual end-to-end verification

Beyond the automated tests: posted genuinely-signed webhook payloads
end-to-end and confirmed, in order - a valid signature is accepted and
returns proper TwiML with the correct content type; an invalid
signature is rejected (403); a missing signature is rejected (403); the
call is logged with the right fields; a subsequent status callback for
the same CallSid updates the same row (not a new one) with the right
duration; and caller-ID lookup correctly matches a differently-
formatted version of a stored phone number (`+1 (555) 123-4567` on the
incoming call against `+15551234567` stored on the contact).

### Test suite

```
$ python -m pytest -q
111 passed, 3 warnings
```

14 new tests in `tests/test_telephony.py`; the full suite re-verified
against real Postgres (`TEST_DATABASE_URL=postgresql://...`) - all 111
pass there too.

### Known limitations, stated plainly - this one has a harder ceiling than the others

Every other "unverified against a live account" limitation in this
project (Groq, Resend) was closeable simply by obtaining a free API
key. This one is not: a genuine end-to-end test (an actual phone call
reaching this app) requires Twilio's servers to reach a **publicly
reachable URL**, and this sandbox has none. Getting a real Twilio
account would let someone configure a phone number's webhook URLs, but
proving a live call actually works still requires the app to be
deployed somewhere public first - a materially bigger step than
"add a key to `.env`," and one that couldn't be completed in this
environment regardless of account access.

What was and wasn't verified, stated precisely: the signature
algorithm is verified correct against the official SDK's own output;
the webhook handlers, idempotency, and caller-ID logic are verified
against realistic, genuinely-signed synthetic payloads. What remains
unverified is whether Twilio's real, live webhook requests match the
synthetic ones used here closely enough in practice - the docs suggest
they should, but "the docs say so" is a weaker claim than this
project's own standard elsewhere, and is named as such rather than
glossed over.

Also not built in this slice: manual call-to-ticket linking UI (the
data model and `crud.link_call_to_ticket` support it; no route/template
calls it yet), outbound calling (Twilio's REST API to place a call, as
opposed to receiving webhooks about one), and voicemail/recording
playback in the UI (recording URLs are stored when present, not
rendered anywhere yet).

### Next milestone

**Email is now fully closed.** The user obtained a real Resend key,
sent a real message through the running app and a genuinely separate
worker process, and confirmed it arrived in their actual inbox - see
the live-verification update above. That was the one real gap left on
the email side.

**Phone remains open**, and has a harder ceiling than email did: live
verification needs both a real Twilio account AND a public deployment
for Twilio's servers to reach, which is a materially bigger step than
adding a key. Reasonable next steps from here: pursue that public
deployment to close the phone gap too, or move to a different part of
the brief (Milestone 4+, or hardening/polish on what's already built).
