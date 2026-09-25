# TicketBase Runbook

Short operational procedures. "Configured" is not "verified": a setting in
`render.yaml` is not evidence that a service is running.

## Health
- `GET /live` - process is up. Never touches the database.
- `GET /ready` - 200 only if the database answers, the schema is at this
  build's migration head, and the retrieval index is loaded; otherwise 503
  with a `checks` object naming the failure. Also reports outbox backlog
  (`waiting`, `oldest_waiting_seconds`, `failed`).
- Point Render's health check at `/ready`.

## Deploy
1. Confirm HUMAN_TASKS H1 (SECRET_KEY and API_KEY, each >= 32 chars).
2. Push to `main`. CI: dependency audit, tests on SQLite and PostgreSQL,
   migration checks.
3. Migrations:
   - Paid plan: Pre-Deploy Command `alembic upgrade head`.
   - Free plan (no pre-deploy): set `RUN_MIGRATIONS_ON_START=true` and run
     exactly one web instance - concurrent instances would race to migrate.
4. After deploy: `curl https://<host>/ready` must return 200 with `"schema": "ok"`.

## "Refusing to start" on deploy
Intended behaviour: a required setting is missing or weak, and the error
names it. Fix it in Render's Environment tab.

## /ready returns 503 "not at migration head"
Migrations didn't run. Check the pre-deploy log, or on the free plan set
`RUN_MIGRATIONS_ON_START=true`, then redeploy.

## Rollback
1. Render -> Web Service -> Events -> previous successful deploy -> Rollback.
2. Migrations are not reversed. All migrations so far are additive or a
   one-way cleanup that older code tolerates, so code-only rollback is safe
   for the current history. Re-check before any migration that drops or
   renames a column.
3. After a code-only rollback, the older build's /ready reports "not at
   migration head" because the database is newer. Expected; it still serves,
   but the health check will fail - temporarily point it at `/live` or
   roll forward.

## Backups
- Render free Postgres has no backups and expires after 30 days.
- Until a paid database is approved (HUMAN_TASKS H6), dump manually:
  `pg_dump "$EXTERNAL_DATABASE_URL" --format=custom -f ticketbase-$(date +%F).dump`
- Prove a dump is usable by restoring into a scratch database:
  `createdb tb_restore && pg_restore --no-owner -d tb_restore ticketbase-DATE.dump`
  then run the app against it and check `/ready`.
- Restore drill status: performed locally on PostgreSQL 16 only (see
  PROGRESS.md); not yet against a Render-hosted database.

## Secrets
Enter secrets only in Render's dashboard. If one is exposed (chat, commit,
screenshot), rotate it at the provider first, then update Render.
