"""
Database connection setup.

Defaults to SQLite (zero setup - just works) so you can start immediately.
Set DATABASE_URL env var to a Postgres connection string later when you're
ready to add pgvector for Project 2 (SupportRAG).

Example Postgres URL (once you have Docker Compose running Postgres):
    postgresql://ticketbase:ticketbase@localhost:5432/ticketbase
"""
import os
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, declarative_base

DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./ticketbase.db")

# SQLite needs this special arg for use with FastAPI's threaded requests.
# Postgres doesn't need it - this line is a no-op if you're on Postgres.
connect_args = {"check_same_thread": False} if DATABASE_URL.startswith("sqlite") else {}

engine = create_engine(DATABASE_URL, connect_args=connect_args)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

Base = declarative_base()


def get_db():
    """FastAPI dependency - gives each request its own DB session, closes it after."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
