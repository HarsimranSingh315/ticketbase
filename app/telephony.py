"""
Twilio webhook signature validation.

Uses Twilio's own maintained `RequestValidator`, not a hand-rolled
HMAC-SHA1 implementation - a deliberate choice, made AFTER writing and
verifying a manual implementation first (see the git history / this
file's own commit message for that verification: the manual version
was byte-for-byte correct against the SDK's own output). Switched to
the SDK anyway because Twilio's own webhook-security docs explicitly
warn that manual implementations have real, documented failure modes
that only their library tracks going forward: empty-value parameters
being silently dropped by some form-parsing libraries (but included in
Twilio's signature), and "evolving parameter sets" Twilio adds without
notice. This is the same reasoning that led this project to use
argon2-cffi instead of hand-rolled password hashing (app/security.py) -
trust a maintained library for a vendor-specific, evolving security
contract, even when a first-principles implementation is verified
correct today.

This is the ONLY thing standing between "anyone on the internet can
POST fake call data to these endpoints" and "only Twilio, holding our
real auth token, can" - webhook routes take no session, no API key,
and no CSRF token (Twilio's servers call them directly, not a browser
or an authenticated API client), so this validation is the entire
security boundary for this integration.
"""
from twilio.request_validator import RequestValidator


def validate_twilio_signature(url: str, params: dict, signature: str, auth_token: str) -> bool:
    """
    Returns True only for a genuine match. Fails closed on every edge
    case: no auth token configured, or no signature header sent, both
    return False before ever asking the validator - an empty auth
    token would otherwise let a blank/missing signature "validate"
    against it, which is exactly backwards for a security check.
    """
    if not auth_token or not signature:
        return False
    validator = RequestValidator(auth_token)
    return validator.validate(url, params, signature)
