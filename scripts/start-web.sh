#!/bin/sh
# Container entrypoint for the web service.
#
# RUN_MIGRATIONS_ON_START=true runs `alembic upgrade head` before serving.
# Use it ONLY when the platform can't run a pre-deploy step (Render's free
# plan) and exactly one web instance runs - concurrent instances would race
# to migrate. With a pre-deploy command configured, leave it unset.
set -eu
if [ "${RUN_MIGRATIONS_ON_START:-false}" = "true" ]; then
    echo "start-web: running database migrations before startup"
    alembic upgrade head
fi
exec uvicorn app.main:app --host 0.0.0.0 --port "${PORT:-8000}" --proxy-headers --forwarded-allow-ips="${FORWARDED_ALLOW_IPS:-127.0.0.1}"
