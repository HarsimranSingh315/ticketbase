"""
Security primitives: password hashing and random token generation.

Deliberately thin - this module does not implement its own crypto, it
wraps well-maintained libraries (argon2-cffi, Python's own `secrets`
module) exactly as the brief requires ("use a maintained authentication
solution... maintained Argon2id password library"). Nothing here should
ever need to roll its own hashing or randomness.
"""
import hashlib
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


def hash_token(raw_token: str) -> str:
    """
    Hashes a session ID or invite token before it's stored - an
    external review found these were being stored as plain, directly
    usable bearer secrets: anyone reading the sessions or invites table
    (a DB leak, a backup, an overly-curious query) could impersonate any
    logged-in user or redeem any pending invite immediately, no cracking
    needed, unlike the already Argon2id-hashed passwords.

    Deliberately SHA-256, not Argon2id: Argon2id is deliberately slow
    and memory-hard specifically to resist brute-forcing a LOW-entropy
    human password. A session/invite token is already a 256-bit random
    value from generate_token() above - astronomically higher entropy
    than any password - so a slow hash buys no real security here, only
    real cost: session lookups happen on every authenticated request,
    and Argon2id in that hot path would measurably slow down the whole
    app for no benefit. A fast cryptographic hash is the standard,
    correct choice for hashing already-high-entropy secrets.
    """
    return hashlib.sha256(raw_token.encode("utf-8")).hexdigest()


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
