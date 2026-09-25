"""
Two separate auth mechanisms for two separate audiences - this is a
deliberate design decision, not an accident of incremental patching:

1. `require_api_key` - a single shared secret for MACHINE clients (the
   JSON API, the CLI). Appropriate for a script or another service
   calling in; a shared secret has no concept of "which agent did this"
   and isn't meant to.

2. `require_agent` / `require_role` - real per-user session auth for
   HUMAN agents using the browser UI. This is what Milestone 1 adds:
   before this, the browser UI either had no protection at all, or (as
   a stopgap) reused the same shared API key in a way browsers can't
   practically send - see docs/current-state.md for that history. Real
   sessions are what actually lets the app know WHO confirmed a
   category, not just THAT a request had a valid secret.

Session auth here is opaque server-side sessions (a random token that's
just a database lookup key), not JWTs - see the Session model's
docstring in app/models.py for why that's deliberate. CSRF protection
(`require_csrf`) exists because this app now trusts an ambient browser
cookie for state-changing requests, which is exactly the situation CSRF
protection exists for - see OWASP's CSRF cheat sheet.
"""
import hmac
from typing import Optional

from fastapi import Header, HTTPException, status, Depends, Request, Form
from sqlalchemy.orm import Session as DBSession

from app.config import get_settings, Settings
from app.database import get_db
from app import crud
from app.models import User, UserRole
from app.security import verify_csrf_token

SESSION_COOKIE_NAME = "session_id"


class AuthRedirect(Exception):
    """Raised when a browser route needs a logged-out user sent to
    /login, rather than shown a raw 401. Caught by a handler registered
    in main.py (where the FastAPI `app` instance lives)."""
    def __init__(self, next_path: str):
        self.next_path = next_path


def require_api_key(
    x_api_key: Optional[str] = Header(default=None),
    settings: Settings = Depends(get_settings),
) -> None:
    """
    Machine-client credential check for the JSON write routes.

    The empty-key bypass exists ONLY for explicit development/test
    environments (so the local suite can run without a secret). It used
    to apply in every environment, which meant a production deployment
    without API_KEY accepted anonymous POST /tickets and PATCH status -
    a P0 finding, reproduced directly before this fix. Strict
    environments can't start without API_KEY at all (see
    config.validate_settings); this second check is defense in depth
    in case validation is ever bypassed.
    """
    if not settings.api_key:
        if settings.environment in ("development", "test"):
            return
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="API credentials are not configured on this server.",
        )
    if not x_api_key or not hmac.compare_digest(x_api_key, settings.api_key):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing or invalid API key. Send it as the X-API-Key header.",
        )


def get_current_user(request: Request, db: DBSession = Depends(get_db)) -> Optional[User]:
    """Returns the logged-in User for this request's session cookie, or
    None if there isn't a valid one. Never raises - callers decide
    whether the absence of a user is acceptable."""
    session_id = request.cookies.get(SESSION_COOKIE_NAME)
    if not session_id:
        return None
    session = crud.get_active_session(db, session_id)
    if session is None:
        return None
    crud.touch_session(db, session)
    user = crud.get_user(db, session.user_id)
    if user is None or not user.is_active:
        return None
    return user


def require_agent(request: Request, db: DBSession = Depends(get_db)) -> User:
    """Any logged-in, active user - regardless of role. Redirects to
    /login (not a raw 401) since this guards browser-rendered pages."""
    user = get_current_user(request, db)
    if user is None:
        raise AuthRedirect(next_path=request.url.path)
    return user


def require_session_or_api_key(
    request: Request,
    x_api_key: Optional[str] = Header(default=None),
    db: DBSession = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> None:
    """
    Protects private JSON routes that the browser's OWN same-origin JS
    also calls (app.js's AJAX suggestion request uses the session
    cookie it already has) - these must accept EITHER a real logged-in
    session OR a valid API key, and reject everything else. Deliberately
    does NOT no-op when settings.api_key is empty, unlike
    require_api_key above: that permissive fallback on these specific
    routes was a real, live P0 finding (unauthenticated GET /tickets,
    GET /tickets/{id}, GET /tickets/{id}/related, and POST
    /tickets/{id}/suggest all returned real ticket data to anonymous
    requests - see docs/current-state.md's production-readiness
    section for how this was found and fixed). Without a configured API
    key, only a real session works here; with one configured, either
    does - never "neither, and it's still fine."
    """
    if x_api_key and settings.api_key and hmac.compare_digest(x_api_key, settings.api_key):
        return
    if get_current_user(request, db) is not None:
        return
    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Authentication required - log in via the browser, or send a valid X-API-Key header.",
    )


def require_role(*roles: UserRole):
    """
    Factory: require_role(UserRole.admin, UserRole.agent) etc. Layers on
    top of require_agent, so an unauthenticated request still redirects
    to /login rather than a confusing 403 that doesn't explain WHY.
    """
    def dependency(user: User = Depends(require_agent)) -> User:
        if user.role not in roles:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Your role ({user.role.value}) doesn't have permission to do that.",
            )
        return user
    return dependency


def require_csrf(
    request: Request,
    csrf_token: str = Form(...),
    user: User = Depends(require_agent),
    settings: Settings = Depends(get_settings),
) -> None:
    """
    Verifies the hidden csrf_token form field against this request's own
    session cookie. Only meaningful for cookie-authenticated state
    changes - the JSON API (using X-API-Key, not a cookie) has no
    ambient credential for a forged cross-site request to exploit, so
    it doesn't need this (see OWASP's CSRF cheat sheet on when CSRF
    protection applies).
    """
    session_id = request.cookies.get(SESSION_COOKIE_NAME)
    if not session_id or not verify_csrf_token(csrf_token, session_id, settings.secret_key):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Invalid or missing CSRF token. Reload the page and try again.",
        )


def csrf_token_for_template(request: Request, settings: Settings) -> Optional[str]:
    """
    For rendering the hidden csrf_token field in a form. Returns None
    if there's no session (nothing to bind the token to) - a template
    only needs this when the user is actually logged in.
    """
    session_id = request.cookies.get(SESSION_COOKIE_NAME)
    if not session_id:
        return None
    from app.security import compute_csrf_token
    return compute_csrf_token(session_id, settings.secret_key)
