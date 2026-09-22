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


--- Outbound calling (below) ---

Two adapters behind one interface, same pattern as app/mail.py's
LocalSinkAdapter/ResendAdapter:
- LocalSinkCallAdapter (default, no config needed): records the call
  attempt as a real Call row, with no actual network call to Twilio -
  genuinely useful for local dev/demo without needing any Twilio
  account at all, same reasoning as the mail local sink.
- TwilioCallAdapter (only when TWILIO_ACCOUNT_SID/AUTH_TOKEN AND
  PUBLIC_BASE_URL are all configured): places a REAL call via Twilio's
  REST API. Written and unit-tested with a mocked Twilio client, but
  NOT verified against a live account in this project - same honesty
  as ResendAdapter before a real key existed for it, and the same
  additional, harder limitation already documented for the webhook-
  receiving side: even with a real Twilio account, this specific
  adapter also needs PUBLIC_BASE_URL to be a real, Twilio-reachable
  URL, which this sandboxed environment cannot provide either way.
"""
from dataclasses import dataclass
from typing import Optional

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


@dataclass
class OutboundCallResult:
    success: bool
    call_sid: Optional[str] = None
    error: Optional[str] = None


class LocalSinkCallAdapter:
    """No real call is placed. Returns a fake, clearly-labeled call
    SID so the rest of the app (Call row creation, UI) behaves
    identically whether the call is real or simulated - the ONLY
    difference is whether a real phone rings. Accepts the same
    signature as TwilioCallAdapter (ignoring twiml_url, which is
    meaningless without a real call) so callers can use either adapter
    uniformly, same pattern as app/mail.py's two adapters."""

    def place_call(self, to_number: str, from_number: str, twiml_url: Optional[str] = None) -> OutboundCallResult:
        import uuid
        return OutboundCallResult(success=True, call_sid=f"local-call-{uuid.uuid4().hex[:16]}")


class TwilioCallAdapter:
    """Places a real call via Twilio's REST API. `twiml_url` must be a
    real, Twilio-reachable URL - Twilio fetches it for instructions the
    moment the call connects. Reuses the existing inbound
    /webhooks/twilio/voice endpoint's TwiML (a generic support
    greeting) rather than building a second, parallel TwiML response
    for outbound calls - the greeting is equally correct either way."""

    def __init__(self, account_sid: str, auth_token: str):
        from twilio.rest import Client
        self.client = Client(account_sid, auth_token)

    def place_call(self, to_number: str, from_number: str, twiml_url: Optional[str] = None) -> OutboundCallResult:
        if not twiml_url:
            return OutboundCallResult(success=False, error="No twiml_url configured (PUBLIC_BASE_URL is required for real outbound calls)")
        try:
            call = self.client.calls.create(to=to_number, from_=from_number, url=twiml_url)
            return OutboundCallResult(success=True, call_sid=call.sid)
        except Exception as exc:
            # Twilio's own TwilioRestException, plus anything else that
            # could go wrong on a real network call - never let a
            # failed outbound call attempt raise all the way up into a
            # 500; the route always gets a clean, checkable result.
            return OutboundCallResult(success=False, error=str(exc))


def get_call_adapter(settings):
    """LocalSinkCallAdapter unless BOTH Twilio credentials are
    configured - matches app/mail.py's get_mail_adapter pattern."""
    if settings.twilio_account_sid and settings.twilio_auth_token:
        return TwilioCallAdapter(settings.twilio_account_sid, settings.twilio_auth_token)
    return LocalSinkCallAdapter()
