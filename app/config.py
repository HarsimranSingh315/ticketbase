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
    llm_model: str = "openai/gpt-oss-20b"
    llm_timeout_seconds: float = 8.0

    # Session-based agent auth (Milestone 1). SECRET_KEY signs CSRF
    # tokens - if left blank, a random one is generated at process
    # startup as a dev convenience, logged as a clear warning since it
    # means sessions won't survive a restart and won't be consistent
    # across multiple processes. Set a real SECRET_KEY for anything
    # beyond a single local dev process.
    secret_key: str = ""
    session_ttl_hours: int = 12
    invite_ttl_hours: int = 72
    login_rate_limit: str = "10/minute"
    # False for local http:// dev (the default). MUST be true in any real
    # deployment served over HTTPS - a session cookie sent over plain
    # HTTP can be intercepted. Not auto-detected because this process
    # has no reliable way to know if it's behind a TLS-terminating proxy.
    session_cookie_secure: bool = False

    # Bootstrap: since account creation is invite-only (no open signup),
    # something has to create the very first admin. If both of these are
    # set AND no users exist yet in the database, the app creates this
    # one admin account at startup. Leave blank after first use - this
    # is a one-time bootstrap mechanism, not a standing backdoor (it
    # only ever acts when the users table is completely empty).
    bootstrap_admin_email: str = ""
    bootstrap_admin_password: str = ""
    bootstrap_admin_name: str = "Admin"


@lru_cache
def get_settings() -> Settings:
    """
    Cached so Settings() (which reads env vars / .env) only runs once
    per process, not on every request. Tests override this via
    app.dependency_overrides[get_settings], same pattern as get_db.
    """
    settings = Settings()
    if not settings.secret_key:
        import logging
        import secrets as _secrets
        settings.secret_key = _secrets.token_urlsafe(32)
        logging.getLogger("ticketbase").warning(
            "SECRET_KEY not set - generated a random one for this process only. "
            "CSRF tokens will stop validating across a restart, and this is NOT "
            "safe if you ever run more than one process (they'd each get a "
            "different key). Set SECRET_KEY in .env for anything beyond a "
            "single local dev process."
        )
    return settings
