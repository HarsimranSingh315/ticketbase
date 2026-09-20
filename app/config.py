"""
Centralized application configuration.

Everything here can be overridden via environment variables or a `.env`
file (see `.env.example`) - nothing production-relevant should be a
hardcoded literal buried in some other module. This is the single
source of truth for "how is this deployment configured", which matters
once you have more than one environment (local, CI, staging, prod).
"""
from functools import lru_cache
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # Database
    database_url: str = "sqlite:///./ticketbase.db"

    # API auth. Empty string = auth disabled (local/dev/test default).
    # Set TICKETBASE_API_KEY to a real value to require it on write
    # endpoints (POST/PATCH) - see app/auth.py.
    api_key: str = ""

    # SupportRAG tuning
    rag_top_k: int = 3
    rag_confidence_threshold: float = 0.25

    # Rate limiting (requests per minute) for the /suggest endpoint,
    # which is the most compute-heavy route in the app.
    suggest_rate_limit: str = "20/minute"

    # Pagination
    default_page_size: int = 20
    max_page_size: int = 100

    # Logging
    log_level: str = "INFO"


@lru_cache
def get_settings() -> Settings:
    """
    Cached so Settings() (which reads env vars / .env) only runs once
    per process, not on every request. Tests override this via
    app.dependency_overrides[get_settings], same pattern as get_db.
    """
    return Settings()
