"""
Tests for the security hardening pass done before public deployment:
security headers on every response, API docs disabled by default, and
SECRET_KEY becoming a hard startup failure in production instead of a
warning.

Everything here was first verified manually - including actually
importing app.config with ENVIRONMENT=production and no SECRET_KEY set
and confirming it raises, then confirming a real key lets it start, and
confirming plain local dev (no ENVIRONMENT set) is completely
unaffected - before being written as permanent tests.
"""
import pytest


def test_security_headers_present_on_every_response(client):
    resp = client.get("/login")
    assert resp.headers.get("X-Content-Type-Options") == "nosniff"
    assert resp.headers.get("X-Frame-Options") == "DENY"
    assert "max-age=" in resp.headers.get("Strict-Transport-Security", "")
    assert resp.headers.get("Referrer-Policy") == "same-origin"
    csp = resp.headers.get("Content-Security-Policy", "")
    assert "default-src 'self'" in csp
    assert "frame-ancestors 'none'" in csp


def test_security_headers_present_on_json_api_responses_too(client):
    """Not just HTML pages - the middleware wraps every route."""
    resp = client.get("/health")
    assert resp.headers.get("X-Frame-Options") == "DENY"


def test_api_docs_disabled_by_default(client):
    assert client.get("/docs").status_code == 404
    assert client.get("/redoc").status_code == 404
    assert client.get("/openapi.json").status_code == 404


def test_api_docs_enabled_when_configured(monkeypatch):
    """Confirms the toggle actually works, not just that the default is off."""
    monkeypatch.setenv("ENABLE_API_DOCS", "true")
    from app.config import Settings
    settings = Settings(database_url="sqlite:///:memory:")
    assert settings.enable_api_docs is True


def test_secret_key_missing_in_production_refuses_to_start(monkeypatch):
    monkeypatch.setenv("ENVIRONMENT", "production")
    monkeypatch.delenv("SECRET_KEY", raising=False)
    from app.config import get_settings
    get_settings.cache_clear()
    with pytest.raises(RuntimeError, match="Refusing to start"):
        get_settings()
    get_settings.cache_clear()


STRONG = "s" * 48


def test_production_with_all_required_settings_starts_and_forces_secure_cookies(monkeypatch):
    monkeypatch.setenv("ENVIRONMENT", "production")
    monkeypatch.setenv("SECRET_KEY", STRONG)
    monkeypatch.setenv("API_KEY", "k" * 48)
    monkeypatch.setenv("SESSION_COOKIE_SECURE", "false")  # explicitly wrong on purpose
    from app.config import get_settings
    get_settings.cache_clear()
    settings = get_settings()
    assert settings.secret_key == STRONG
    assert settings.session_cookie_secure is True  # enforced, not trusted
    get_settings.cache_clear()


@pytest.mark.parametrize("env", ["production", "staging"])
def test_strict_environment_without_api_key_refuses_to_start(monkeypatch, env):
    """S1 root cause: production previously started without API_KEY and
    accepted anonymous machine writes."""
    monkeypatch.setenv("ENVIRONMENT", env)
    monkeypatch.setenv("SECRET_KEY", STRONG)
    monkeypatch.delenv("API_KEY", raising=False)
    from app.config import get_settings, InsecureConfigurationError
    get_settings.cache_clear()
    with pytest.raises(InsecureConfigurationError, match="API_KEY"):
        get_settings()
    get_settings.cache_clear()


def test_production_with_short_secret_key_refuses_to_start(monkeypatch):
    monkeypatch.setenv("ENVIRONMENT", "production")
    monkeypatch.setenv("SECRET_KEY", "too-short")
    monkeypatch.setenv("API_KEY", "k" * 48)
    from app.config import get_settings, InsecureConfigurationError
    get_settings.cache_clear()
    with pytest.raises(InsecureConfigurationError, match="SECRET_KEY"):
        get_settings()
    get_settings.cache_clear()


def test_unrecognized_environment_name_is_rejected_not_treated_as_dev(monkeypatch):
    """A typo like ENVIRONMENT=prod used to silently mean 'development'
    - every production protection off, with no error."""
    monkeypatch.setenv("ENVIRONMENT", "prod")
    from app.config import get_settings, InsecureConfigurationError
    get_settings.cache_clear()
    with pytest.raises(InsecureConfigurationError, match="not recognized"):
        get_settings()
    get_settings.cache_clear()


def test_require_api_key_rejects_when_unconfigured_outside_dev():
    """Defense in depth: even if startup validation were bypassed, an
    empty key must not mean 'open' in a strict environment."""
    from fastapi import HTTPException
    from app.auth import require_api_key
    from app.config import Settings
    s = Settings(environment="production", api_key="", secret_key=STRONG)
    with pytest.raises(HTTPException) as exc:
        require_api_key(x_api_key=None, settings=s)
    assert exc.value.status_code == 503


def test_secret_key_missing_in_development_still_just_warns(monkeypatch):
    """Local dev convenience is unaffected - this must NOT start raising
    for anyone who hasn't set ENVIRONMENT=production."""
    monkeypatch.delenv("ENVIRONMENT", raising=False)
    monkeypatch.delenv("SECRET_KEY", raising=False)
    from app.config import get_settings
    get_settings.cache_clear()
    settings = get_settings()
    assert settings.secret_key  # auto-generated, not empty
    get_settings.cache_clear()


@pytest.fixture
def production_client(client):
    """The real app, with production settings injected (strong keys,
    Secure cookies forced by validate_settings)."""
    from app.main import app
    from app.config import get_settings, Settings, validate_settings
    prod = validate_settings(Settings(environment="production", secret_key=STRONG, api_key="k" * 48,
                                      database_url="sqlite://"))
    app.dependency_overrides[get_settings] = lambda: prod
    yield client
    app.dependency_overrides.pop(get_settings, None)


def test_s1_anonymous_machine_writes_rejected_under_production_config(production_client):
    """The exact P0 reproduction: before the fix both returned 2xx."""
    assert production_client.post("/tickets", json={"description": "anon"}).status_code == 401
    assert production_client.patch("/tickets/1/status", json={"status": "resolved", "version": 1}).status_code == 401


def test_s1_correct_api_key_still_works_under_production_config(production_client):
    r = production_client.post("/tickets", json={"description": "ok"}, headers={"X-API-Key": "k" * 48})
    assert r.status_code == 201


def test_s2_prelogin_cookie_is_secure_under_production_config(production_client, monkeypatch):
    """The helpers read get_settings() directly (not via Depends), so
    patch the cached function for this check too."""
    import app.main as main_mod
    from app.config import Settings, validate_settings
    prod = validate_settings(Settings(environment="production", secret_key=STRONG, api_key="k" * 48,
                                      database_url="sqlite://"))
    monkeypatch.setattr(main_mod, "get_settings", lambda: prod)
    r = production_client.get("/login")
    set_cookie = r.headers.get("set-cookie", "")
    assert "prelogin_csrf=" in set_cookie
    assert "Secure" in set_cookie and "HttpOnly" in set_cookie and "Path=/" in set_cookie


def test_authenticated_pages_are_not_cacheable(admin_client):
    for path in ["/", "/customers", "/kb"]:
        assert admin_client.get(path).headers.get("cache-control") == "no-store", path


def test_static_assets_remain_cacheable(client):
    r = client.get("/static/style.css")
    assert r.status_code == 200 and r.headers.get("cache-control") != "no-store"


def test_invite_page_sends_no_referrer(client):
    assert client.get("/accept-invite?token=whatever").headers.get("referrer-policy") == "no-referrer"


def test_unknown_email_login_still_performs_password_verification(client, monkeypatch):
    """Timing-oracle regression: an unknown email must still cost one
    Argon2 verification, exactly like a wrong password does."""
    import app.security as sec
    calls = []
    real = sec.verify_password
    monkeypatch.setattr(sec, "verify_password", lambda pw, h: calls.append(h) or real(pw, h))
    from tests.conftest import _login
    r = _login(client, "nobody-here@example.com", "whatever")
    assert r.status_code == 401
    assert len(calls) == 1
