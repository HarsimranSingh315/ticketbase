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

    # "development" (default) keeps today's friendly local/test behavior
    # - an unset SECRET_KEY auto-generates one and logs a warning, and
    # interactive API docs are available. Set ENVIRONMENT=production for
    # a real deployment: the app then REFUSES TO START on a missing
    # SECRET_KEY (see get_settings below) instead of silently limping on
    # with a key that changes every restart and breaks CSRF/sessions the
    # moment there's more than one process - a warning is easy to miss
    # in deploy logs, a startup crash is not.
    environment: str = "development"

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

    # FastAPI's interactive API docs (/docs, /redoc) are OFF by default -
    # every route already requires real auth to do anything, so this
    # isn't a break-in risk either way, but there's no reason to publish
    # a map of the API's full surface to the open internet. Set
    # ENABLE_API_DOCS=true for local development if you want them.
    enable_api_docs: bool = False

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

    # Milestone 2: outbound email. Empty resend_api_key (the default)
    # means the LocalSinkAdapter is used - nothing ever leaves the
    # machine, "sent" mail lands in the local_sink_emails table where
    # it can be inspected. Set RESEND_API_KEY to switch to the real
    # Resend adapter (app/mail.py) - written and unit-tested with
    # mocks, but not verified against a live account in this project
    # (no key available), same honesty as the Groq LLM integration
    # before its key existed.
    resend_api_key: str = ""
    mail_from_address: str = "support@ticketbase.local"
    mail_from_name: str = "TicketBase Support"

    # Outbox worker tuning (see worker.py).
    outbox_lease_seconds: int = 60
    outbox_max_attempts: int = 5
    outbox_poll_interval_seconds: float = 2.0

    # Milestone 3: telephony (Twilio webhooks). Empty twilio_auth_token
    # means webhook signature validation will reject everything (fails
    # closed, not open - see app/telephony.py) until a real token is
    # configured. There is no "local sink" equivalent for phone calls -
    # unlike email, a call can't be faked locally in a meaningful way,
    # so this integration is exercised in tests via correctly-signed
    # simulated webhook payloads instead (see tests/test_telephony.py).
    twilio_auth_token: str = ""
    twilio_account_sid: str = ""
    twilio_phone_number: str = ""

    # Required for OUTBOUND calling specifically (not the webhook-
    # receiving side, which needs no config of its own beyond the auth
    # token). Twilio's REST API requires a URL it can fetch TwiML
    # instructions from when the call connects - that URL must be
    # publicly reachable BY TWILIO, which this project cannot provide
    # from a local/sandboxed environment. Left blank, outbound calling
    # fails closed with a clear error rather than attempting an API
    # call Twilio would reject anyway.
    public_base_url: str = ""

    # SLA timers. Deadline = ticket.created_at + the hours for its
    # priority. Deliberately simple (one clock per priority, not
    # business-hours-aware or pausable) - a real production SLA engine
    # would need those refinements, but this gives a genuine, useful
    # "is this overdue" signal without that complexity.
    sla_high_priority_hours: float = 4.0
    sla_medium_priority_hours: float = 24.0
    sla_low_priority_hours: float = 72.0
    # Poll interval for sla_check.py's continuous mode (see that file).
    sla_check_poll_interval_seconds: float = 60.0


STRICT_ENVIRONMENTS = {"production", "staging"}
KNOWN_ENVIRONMENTS = {"development", "test"} | STRICT_ENVIRONMENTS
MIN_SECRET_LENGTH = 32


class InsecureConfigurationError(RuntimeError):
    """Raised at startup when a strict environment is missing a setting
    it cannot safely run without. A crash here is deliberate: a warning
    in deploy logs is easy to miss, a service that refuses to start is not."""


def validate_settings(settings: "Settings") -> "Settings":
    """
    Enforces, in code rather than documentation, the settings a real
    deployment needs. Called by get_settings on every process start.

    Development/test keep their conveniences (auto-generated SECRET_KEY,
    optional API_KEY) - but ONLY when the environment is explicitly one
    of those names. An unrecognized value (e.g. a typo like "prod") is
    rejected outright rather than silently treated as permissive dev
    mode, which was the realistic way a live deployment could end up
    unprotected.
    """
    env = settings.environment.strip().lower()
    settings.environment = env
    if env not in KNOWN_ENVIRONMENTS:
        raise InsecureConfigurationError(
            f"ENVIRONMENT={settings.environment!r} is not recognized. Use one of: "
            f"{', '.join(sorted(KNOWN_ENVIRONMENTS))}."
        )

    if env in STRICT_ENVIRONMENTS:
        problems = []
        if len(settings.secret_key or "") < MIN_SECRET_LENGTH:
            problems.append(f"SECRET_KEY must be set and at least {MIN_SECRET_LENGTH} characters")
        if len(settings.api_key or "") < MIN_SECRET_LENGTH:
            problems.append(
                f"API_KEY must be set and at least {MIN_SECRET_LENGTH} characters - without it, "
                "machine write routes (POST /tickets, PATCH status) would have no credential to check"
            )
        if problems:
            raise InsecureConfigurationError(
                f"Refusing to start with ENVIRONMENT={env}: " + "; ".join(problems)
                + '. Generate values with: python -c "import secrets; print(secrets.token_urlsafe(48))"'
            )
        # Enforced, not merely documented: strict environments are
        # always served over HTTPS (Render terminates TLS), so a session
        # cookie without Secure is never correct there.
        settings.session_cookie_secure = True
        return settings

    if not settings.secret_key:
        import logging
        import secrets as _secrets
        settings.secret_key = _secrets.token_urlsafe(32)
        logging.getLogger("ticketbase").warning(
            "SECRET_KEY not set - generated a random one for this process only "
            "(development mode). Sessions and CSRF tokens will not survive a restart."
        )
    return settings


@lru_cache
def get_settings() -> Settings:
    """Cached per process. Tests override via app.dependency_overrides."""
    return validate_settings(Settings())
