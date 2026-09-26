# Release Progress

Resume point for the production-release effort. Keep this short; history
lives in git and `docs/current-state.md`.

## Baseline (start of release effort)
- Python 3.12.3, git `4dec779`, clean tree.
- SQLite: 186 passed / 5 skipped. PostgreSQL 16: not re-run at baseline.
- Browser UI inspection: headless Chromium available at /opt/pw-browsers (Node Playwright 1.56).
  Server and browser must run in the SAME tool call (background processes don't persist).

## Review findings - status
Each was reproduced or confirmed in source before fixing.

| ID | Status | Commit | Evidence |
|---|---|---|---|
| S1 anonymous writes in prod | FIXED | 208c400 | reproduced 201/200; HTTP-level regression tests |
| S2 cookie Secure/attributes | FIXED | 208c400 | Set-Cookie asserted in test |
| B1 invalid status bricks ticket | FIXED | 208c400 | reproduced LookupError; CRUD + route tests |
| B5 blank/malformed input | FIXED | 208c400 | parametrized tests |
| B2 cleanup overwrites completion | FIXED | c39c02f | deterministic interleaving test |
| B6 call status regression | FIXED | c39c02f | + found `initiated` enum crash |
| B7 LLM shape escapes fallback | FIXED | c39c02f | 6 malformed shapes tested |
| B8 CSP blocks delete confirm | FIXED | c39c02f | server check + template guard test |
| B10 unencoded search URLs | FIXED | c39c02f | encoded-output test |
| Legacy plaintext tokens | FIXED | 67a2e30 | migration run on Postgres with legacy rows |
| B3 KB index per-process | FIXED | this commit | two independent process states, edit+delete visibility tested |
| B4 KB rebuild failure after commit | FIXED | this commit | real unindexable corpus rejected, rolled back, input preserved; startup degrades safely |
| B9 pagination | FIXED | a3762fd | backend existed but no template rendered it; walk-every-page test |
| B11 conflict discards draft | FIXED | B11 commit | preserve/compare/reapply/discard; approve hidden while unsaved |
| B12 suggestion retry/timeout | FIXED | f79dc3e | verified in headless Chromium |
| Suggestion CSRF | FIXED | bd935e4 | forged/missing token tests |
| Cache-Control no-store | FIXED | bd935e4 | header tests |
| Login timing (unknown user) | FIXED | bd935e4 | verification-count test |

## Latest verified test results
- SQLite: 300 passed / 8 skipped / 1 xfailed (known limitation).
- PostgreSQL 16: 308 passed / 1 xfailed.
- Fresh venv from lock files: 227 passed (before B3/B4 tests were added).
- pip-audit (runtime lock): no known vulnerabilities, 2026-09-25.
- Secret scan: only local/CI placeholders; no .env ever committed.
- Restore drill: local PostgreSQL 16 only - passed.
- RESOLVED INTERMITTENT: SQLite outbox barrier test failed ~3% of runs
  (measured 2/60; thread hit "cannot commit - no transaction is active" on
  SQLite's shared connection). Now PostgreSQL-only; 0/40 failures there.

## Operations done
- /live and /ready (503 on DB down or schema behind; KB index degraded is
  reported but non-fatal). render.yaml, scripts/start-web.sh, RUNBOOK.md.
- Locked dependencies; CI installs the dev lock and runs pip-audit.

## Findings noted for Phase 4 (from real-browser runs)
- Google Fonts load from a third party on every page (visitor IPs sent to
  Google; blocked in this sandbox, so screenshots use fallback fonts).
  Self-host the fonts and drop fonts.googleapis.com from the CSP.
- "Match score" hint text looks low-contrast - MEASURE contrast, don't guess.
- Customer link on ticket page is a raw numeric "Customer ID" box (the
  brief asks for searchable customer selection).
- Corrected claim: a suggestion screenshot looked faded; it was captured
  mid fade-in animation (opacity 1 once settled). Not a defect.

## Phase 3 status
- DONE: internal notes + conversation timeline (structural boundary, tests
  drive real email/AI/API paths, mutation-checked guard).
- DONE: My tickets / Unassigned queues; assignment target validation.
- DONE (found via browser): header overflowed every signed-in page at 390px.
- DONE: user administration (deactivate/reactivate, role change, sign out
  everywhere, last-admin protection with a mutation-checked row lock),
  own-password change, atomic invite acceptance.
- BLOCKED on owner (HUMAN_TASKS H8): inbound customer email
  (reply ingestion/threading) and password-reset email need a verified
  provider domain. Not started; will not be faked.

## Phase 4 status (UI/UX)
- Tooling: axe-core 4.13 (WCAG 2.1 A/AA) + headless Chromium; scripts in
  tests/browser/. Automated checks cover only part of WCAG - evidence, not a
  compliance claim.
- Baseline: contrast failed on 22/22 page views (one token, 2.75:1); 2
  critical unlabelled selects. Now: 0 violations on all 22.
- Done: triage queue table (text priority, owner, customer, age), priority
  override, searchable customer picker (keyboard-verified), self-hosted fonts
  (0 third-party requests), conversation-first ticket page, two-step call,
  plain-language copy.
- Not done / honest gaps: no screen-reader test with a real AT (NVDA/VoiceOver);
  no dark mode (deliberately deferred - not in the brief's required list);
  toast notifications/keyboard shortcuts deferred (full-page reloads remain).

## Phase 5 status (AI and knowledge safety)
- Related tickets rebuilt on a labelled eval set (tests/eval/related_pairs.json):
  old method wrongly matched 5/16 unrelated pairs at 0.30; new 0/16, finds
  10/15. Known paraphrase limitation recorded as a strict xfail.
- Prompt injection: fencing + bounded inputs + output validation (links,
  emails, phones not in the source article -> draft rejected) + human approval.
  Mocked-provider evaluation, mutation-checked. Real-model susceptibility NOT
  measured (needs live calls - owner decision).
- docs/AI_DATA.md: exactly what leaves the system, when, and how to turn it off.
- Already covered earlier: KB revisioning and validated publish (B3/B4), source
  links, similarity labelled as a score, abstention, timeouts and rate limits.

## Next
- Phase 6: release verification - release checklist with passed / failed /
  blocked / not-tested, change summary, remaining risks, startup and
  verification commands, evidence-based recommendation.

## Human tasks
See `HUMAN_TASKS.md`. H1 and H2 block the next deploy.
