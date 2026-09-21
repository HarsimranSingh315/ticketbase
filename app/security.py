"""
Security primitives: password hashing and random token generation.

Deliberately thin - this module does not implement its own crypto, it
wraps well-maintained libraries (argon2-cffi, Python's own `secrets`
module) exactly as the brief requires ("use a maintained authentication
solution... maintained Argon2id password library"). Nothing here should
ever need to roll its own hashing or randomness.
"""
import hmac
import secrets

from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError, InvalidHash

_hasher = PasswordHasher()


def hash_password(password: str) -> str:
    """Argon2id hash, with a random salt baked into the output string
    (argon2-cffi's default) - never store or compare plaintext passwords."""
    return _hasher.hash(password)


def verify_password(password: str, password_hash: str) -> bool:
    """Returns True only for a genuine match. Never raises - a malformed
    or foreign hash is treated as a non-match, not an error, so a
    corrupted row can't be used to crash the login route."""
    try:
        return _hasher.verify(password_hash, password)
    except (VerifyMismatchError, InvalidHash):
        return False


def generate_token(n_bytes: int = 32) -> str:
    """A random, URL-safe, unguessable token - used for both session IDs
    and invite tokens. `secrets.token_urlsafe` is Python's own
    CSPRNG-backed generator, built exactly for this."""
    return secrets.token_urlsafe(n_bytes)


def compute_csrf_token(session_id: str, secret_key: str) -> str:
    """
    A CSRF token derived deterministically from the session ID via
    HMAC, rather than a second randomly-generated value stored
    somewhere - there's nothing extra to persist or look up, and a
    token is only ever valid for the specific session it was derived
    from (so it's automatically invalidated the moment that session is).
    This is the synchronizer-token pattern applied to the constraint of
    "add no new stateful thing to track".
    """
    return hmac.new(secret_key.encode(), session_id.encode(), digestmod="sha256").hexdigest()


def verify_csrf_token(token: str, session_id: str, secret_key: str) -> bool:
    if not token:
        return False
    expected = compute_csrf_token(session_id, secret_key)
    return hmac.compare_digest(token, expected)
