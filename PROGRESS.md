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
- SQLite: 242 passed / 5 skipped (Postgres-only race tests skipped by design).
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

## Findings noted for Phase 4 (from real-browser runs)
- Google Fonts load from a third party on every page (visitor IPs sent to
  Google; blocked in this sandbox, so screenshots use fallback fonts).
  Self-host the fonts and drop fonts.googleapis.com from the CSP.
- "Match score" hint text looks low-contrast - MEASURE contrast, don't guess.
- Customer link on ticket page is a raw numeric "Customer ID" box (the
  brief asks for searchable customer selection).
- Corrected claim: a suggestion screenshot looked faded; it was captured
  mid fade-in animation (opacity 1 once settled). Not a defect.

## Next up
1. Phase 3: internal notes + unified timeline (with public/internal boundary
   tests), My Tickets / Unassigned views, user admin (deactivate, role
   change, revoke sessions, password change).
2. Phase 4: screenshot-driven UI pass at desktop and mobile widths.
3. Phase 5: AI safety (prompt-injection handling, eval set, provider docs).

## Human tasks
See `HUMAN_TASKS.md`. H1 and H2 block the next deploy.
