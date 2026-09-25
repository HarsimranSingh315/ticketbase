# TicketBase app image. Paired with docker-compose.yml, which also runs
# Postgres and a separate one-shot migration step - together, `docker
# compose up` runs the whole stack correctly, not just the database.
FROM python:3.12-slim

WORKDIR /app

# Install dependencies first (separate layer) so code changes don't
# invalidate the dependency-install cache on every rebuild.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Non-root: running the app process as root inside the container is an
# unnecessary privilege - if the process is ever compromised, root
# inside the container is a meaningfully worse outcome than a
# dedicated unprivileged user has, even though the container boundary
# itself adds separate isolation. This was specifically flagged
# against this project's Dockerfile before being fixed.
RUN useradd --create-home --shell /bin/bash appuser \
    && chown -R appuser:appuser /app
USER appuser

# Deliberately does NOT run migrations here. An earlier version ran
# `alembic upgrade head && uvicorn ...` as the container's own startup
# command - meaning migrations ran on EVERY container start, including
# every replica in a multi-instance deployment starting concurrently
# (a real race condition risk) and every restart, not just real
# releases. Migrations now run as their own one-shot step (see
# docker-compose.yml's `migrate` service) that the app service waits on
# before starting - "run migrations once during release, not in every
# web/worker startup."
CMD ["sh", "scripts/start-web.sh"]
