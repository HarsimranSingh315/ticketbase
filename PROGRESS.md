# Release Progress

Resume point for the production-release effort. Keep this short; history
lives in git and `docs/current-state.md`.

## Baseline (start of release effort)
- Python 3.12.3, git `4dec779`, clean tree.
- SQLite: 186 passed / 5 skipped. PostgreSQL 16: not re-run at baseline.
- Browser UI inspection: **not yet done** (no browser automation run this session).

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
| B9 pagination | OPEN | | |
| B11 conflict discards draft | OPEN | | |
| B12 suggestion retry/timeout | OPEN | | |
| Suggestion CSRF | FIXED | bd935e4 | forged/missing token tests |
| Cache-Control no-store | FIXED | bd935e4 | header tests |
| Login timing (unknown user) | FIXED | bd935e4 | verification-count test |

## Latest verified test results
- SQLite: 234 passed / 5 skipped (Postgres-only race tests skipped by design).
- PostgreSQL 16: 239 passed.
- Fresh venv from lock files: 227 passed (before B3/B4 tests were added).
- pip-audit (runtime lock): no known vulnerabilities, 2026-09-25.
- Secret scan: only local/CI placeholders; no .env ever committed.
- Restore drill: local PostgreSQL 16 only - passed.
- OPEN INTERMITTENT: one unidentified test failure in one SQLite run
  (right after the B3/B4 refactor); 10 consecutive clean runs since.
  Not reproduced, not explained - watch CI.

## Operations done
- /live and /ready (503 on DB down or schema behind; KB index degraded is
  reported but non-fatal). render.yaml, scripts/start-web.sh, RUNBOOK.md.
- Locked dependencies; CI installs the dev lock and runs pip-audit.

## Next up
1. B9 pagination, B11 conflict-draft preservation, B12 suggestion retry/abort.
2. Phase 3: internal notes + timeline, My Tickets/Unassigned, user admin.
3. Phase 4: UI with real browser screenshots (not yet inspected in a browser).
4. Phase 5: AI safety (prompt-injection handling, eval set).

## Human tasks
See `HUMAN_TASKS.md`. H1 and H2 block the next deploy.
