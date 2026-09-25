# Human Tasks

Actions that need you: account access, money, secrets, or a decision only
the owner can make. Never paste a secret into chat - enter it in Render's
dashboard and reply with the confirmation phrase listed instead.

Status: `OPEN` / `DONE` / `DECIDED`.

---

## H1 - Set API_KEY and a strong SECRET_KEY on Render  `OPEN`
- **Priority:** P0 - blocks the next deploy.
- **Why you:** only you can edit Render environment variables. Since commit
  `208c400`, `ENVIRONMENT=production` refuses to start without both, each at
  least 32 characters. This is intentional (finding S1: production previously
  accepted anonymous ticket writes).
- **Steps:**
  1. Locally run twice: `python -c "import secrets; print(secrets.token_urlsafe(48))"`
  2. Render -> Web Service -> Environment. Set `API_KEY` to the first value.
     If `SECRET_KEY` is shorter than 32 characters, replace it with the second.
     Changing SECRET_KEY logs everyone out once; that's expected.
  3. Confirm `ENVIRONMENT=production` is spelled exactly that way
     (`prod` is now rejected rather than silently treated as development).
  4. `SESSION_COOKIE_SECURE` no longer matters - production forces it on.
  5. Save, then push the latest code.
- **Cost:** none.
- **Confirm by replying:** "H1 done - API_KEY set, deploy started".
- **Blocks:** every future deploy.
- **Meanwhile I continue:** all local work.

## H2 - Make migrations run, and trust Render's proxy  `OPEN`
- **Priority:** P0 for the next deploy (it contains migration `b7d2e4f19a60`,
  and production no longer auto-creates tables).
- **Why you:** Render dashboard access, and only you know your plan.
- **Steps:**
  1. Render -> Web Service -> Settings. If a **Pre-Deploy Command** field
     exists (paid plans), set it to exactly `alembic upgrade head`.
  2. If there is NO such field (free plan), add env var
     `RUN_MIGRATIONS_ON_START=true` instead. Keep one instance only.
  3. Add env var `FORWARDED_ALLOW_IPS=*`. Without it every request looks
     like it comes from Render's proxy, so all users share one rate-limit
     bucket - one person's failed logins could lock everyone out.
  4. Change the health check path to `/ready` (it returns 503 when the app
     genuinely can't serve; the old `/health` returned 200 even with the
     database down).
  5. After deploying, visit `https://ticketbase.onrender.com/ready`.
- **Cost:** none.
- **Confirm by replying:** "H2 done - plan is free/paid" and the `/ready`
  response text (it contains no secrets).
- **Blocks:** production schema correctness; meaningful health checks.

## H3 - Remove bootstrap admin credentials  `OPEN`
- **Priority:** P1.
- **Why you:** Render dashboard access.
- **Steps:** delete `BOOTSTRAP_ADMIN_EMAIL` and `BOOTSTRAP_ADMIN_PASSWORD` from
  the Web Service environment once your admin account works.
- **Cost:** none. **Confirm:** "H3 done".
- **Blocks:** nothing technically; it removes a standing credential.

## H4 - Decide: worker and SLA checker on Render  `DECIDED (deferred)`
- You chose to skip the $7/mo Background Worker and the Cron Job for now.
  Consequence, stated plainly: approved emails stay queued and are never
  sent, and SLA breaches aren't recorded in the background. The UI must say
  this clearly (tracked as an engineering item, not a human task).
- **Revisit when:** a real team relies on outbound email.

## H5 - Rotate any credential ever pasted into chat or committed  `OPEN - check`
- **Priority:** P1.
- **Why you:** only you can see your Twilio/Resend/Groq dashboards.
- **Steps:** if a Twilio auth token, Resend key or LLM key was ever pasted
  anywhere outside the provider dashboard or Render, rotate it at the
  provider, then update it in Render.
- **Cost:** none. **Confirm:** "H5 checked - rotated X" or "H5 checked - nothing to rotate".

## H6 - Staging environment and budget  `OPEN - decision`
- **Priority:** P2 (needed for Phase 6 release verification).
- **Why you:** it costs money and needs your approval.
- **What:** a second Render web service + free Postgres with
  `ENVIRONMENT=staging`, used for migration rehearsal, restore drills and
  load checks before production. Approximate cost: $0 on free tiers (with
  sleep/expiry limits) or about $7-13/month for always-on.
- **Confirm by replying:** "H6 approved at $X/month" or "H6 declined".
- **Blocks:** restore drill, staging load test, rollback rehearsal.
- **Meanwhile:** I test against local PostgreSQL 16 and document the gap.

## H7 - Operating decisions  `OPEN - decision`
- **Priority:** P2 (needed before Phase 3 email ingestion and SLA work).
- Support hours and time zone (business-hours SLAs).
- Data retention: how long to keep resolved tickets, call records, audit events.
- Whether external AI drafting may be enabled at all for real customer data,
  and with which provider (the ticket text and a KB excerpt are sent to it).
- **Confirm by replying** with your choices; no secrets involved.
