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

    # Optional LLM step for SupportRAG's drafted response (see app/llm.py).
    # Empty api_key = disabled (default) - the deterministic template in
    # supportrag.py is used instead, exactly as before this feature
    # existed. Defaults target Groq's free, no-credit-card tier
    # (OpenAI-compatible API) - swapping to another OpenAI-compatible
    # provider (OpenRouter, Cerebras, etc.) is just changing these three
    # values, no code change. Free-tier model catalogs are known to
    # change without notice, so a bad/decommissioned model name fails
    # closed (falls back to the template) rather than breaking the
    # feature - see app/llm.py.
    llm_api_key: str = ""
    llm_api_base: str = "https://api.groq.com/openai/v1"
    llm_model: str = "llama-3.3-70b-versatile"
    llm_timeout_seconds: float = 8.0


@lru_cache
def get_settings() -> Settings:
    """
    Cached so Settings() (which reads env vars / .env) only runs once
    per process, not on every request. Tests override this via
    app.dependency_overrides[get_settings], same pattern as get_db.
    """
    return Settings()
