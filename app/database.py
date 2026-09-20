"""
Database connection setup.

Reads DATABASE_URL from centralized config (app/config.py), which in
turn reads it from the environment / .env file. Defaults to SQLite
(zero setup - just works) so you can start immediately. Set
DATABASE_URL to a Postgres connection string to run against Postgres
instead (see docker-compose.yml and .env.example).
"""
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, declarative_base

from app.config import get_settings

settings = get_settings()

# SQLite needs this special arg for use with FastAPI's threaded requests.
# Postgres doesn't need it - this line is a no-op if you're on Postgres.
connect_args = {"check_same_thread": False} if settings.database_url.startswith("sqlite") else {}

engine = create_engine(settings.database_url, connect_args=connect_args)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

Base = declarative_base()


def get_db():
    """FastAPI dependency - gives each request its own DB session, closes it after."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
