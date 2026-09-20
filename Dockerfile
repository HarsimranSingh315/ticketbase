# TicketBase app image. Paired with docker-compose.yml, which also runs
# Postgres - together, `docker compose up` runs the whole stack, not
# just the database.
FROM python:3.12-slim

WORKDIR /app

# Install dependencies first (separate layer) so code changes don't
# invalidate the dependency-install cache on every rebuild.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Applies any pending Alembic migrations, then starts the API. Using
# migrations here (not Base.metadata.create_all) is the production path -
# see the "Migrations" section in README.md for why that distinction
# matters.
CMD ["sh", "-c", "alembic upgrade head && uvicorn app.main:app --host 0.0.0.0 --port 8000"]
