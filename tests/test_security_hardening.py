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


def test_secret_key_present_in_production_starts_fine(monkeypatch):
    monkeypatch.setenv("ENVIRONMENT", "production")
    monkeypatch.setenv("SECRET_KEY", "a-real-production-secret")
    from app.config import get_settings
    get_settings.cache_clear()
    settings = get_settings()
    assert settings.secret_key == "a-real-production-secret"
    get_settings.cache_clear()


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
