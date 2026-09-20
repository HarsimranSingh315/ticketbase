"""
API key authentication.

Off by default: if settings.api_key is empty (the local/dev/test
default), `require_api_key` is a no-op - this is what lets the existing
test suite run without needing to know a secret. Set API_KEY in the
environment (or .env) to require every write request to send a matching
`X-API-Key` header.

This is intentionally simple (a single shared key, not per-user tokens
or OAuth) - appropriate for a small internal tool / portfolio project.
A real multi-tenant product would need per-user auth instead; that's a
much bigger change and out of scope here, same spirit as the "Non-goals"
section in the README.
"""
from fastapi import Header, HTTPException, status, Depends

from app.config import get_settings, Settings


def require_api_key(
    x_api_key: str | None = Header(default=None),
    settings: Settings = Depends(get_settings),
) -> None:
    if not settings.api_key:
        # Auth disabled - local/dev/test default.
        return
    if x_api_key != settings.api_key:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing or invalid API key. Send it as the X-API-Key header.",
        )
