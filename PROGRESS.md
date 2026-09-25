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
| B3 KB index per-process | OPEN | | |
| B4 KB rebuild failure after commit | OPEN | | |
| B9 pagination | OPEN | | |
| B11 conflict discards draft | OPEN | | |
| B12 suggestion retry/timeout | OPEN | | |
| Suggestion CSRF | OPEN | | |
| Cache-Control no-store | OPEN | | |
| Login timing (unknown user) | OPEN | | |

## Latest verified test results
- SQLite: 219 passed / 5 skipped (the 5 are Postgres-only race tests, skipped by design).
- PostgreSQL 16: 224 passed (commit c39c02f).

## Next up
1. Suggestion-route CSRF, Cache-Control no-store, login timing equalization.
2. /live and /ready endpoints; render.yaml; split dev/prod dependencies + lock.
3. B3/B4 KB revision + validated index publish.
4. Phase 3 workflow; Phase 4 UI with real browser screenshots.

## Human tasks
See `HUMAN_TASKS.md`. H1 and H2 block the next deploy.
