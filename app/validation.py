"""
Shared input normalization and validation.

Browser `required`/`type="email"` attributes are a convenience, not a
control - any HTTP client can bypass them. Every value that reaches the
database from a form or JSON body goes through these functions, so the
UI, the JSON API and the CRUD layer agree on what "valid" means.

Findings this closes (verified by reproduction before fixing):
- B1: the UI status route committed arbitrary strings; on SQLite the
  bad enum value was stored and every later read of that ticket raised.
- B5: whitespace-only customer names, malformed recipient emails, and
  blank-after-trim subjects/bodies were accepted.
"""
import re
from typing import Optional

from app.models import TicketStatus


class ValidationError(ValueError):
    """A user-correctable input problem. `field` lets forms show the
    message next to the right input instead of a generic error."""

    def __init__(self, field: str, message: str):
        super().__init__(f"{field}: {message}")
        self.field = field
        self.message = message


# Deliberately conservative: local@domain.tld, no whitespace, one @.
# Full RFC 5322 is neither needed nor desirable for outbound support mail.
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
# Control characters other than tab/newline have no place in form text
# and can corrupt logs or email headers.
_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")

MAX_NAME = 200
MAX_SUBJECT = 200
MAX_BODY = 20_000
MAX_DESCRIPTION = 10_000
MAX_CATEGORY = 64
MAX_EMAIL = 320


def clean_text(value: Optional[str], field: str, max_len: int, required: bool = True, single_line: bool = False) -> str:
    if value is None:
        value = ""
    value = _CONTROL_RE.sub("", value).strip()
    if single_line and ("\n" in value or "\r" in value):
        raise ValidationError(field, "must be a single line")
    if required and not value:
        raise ValidationError(field, "cannot be blank")
    if len(value) > max_len:
        raise ValidationError(field, f"must be at most {max_len} characters")
    return value


def validate_email(value: Optional[str], field: str = "email") -> str:
    value = clean_text(value, field, MAX_EMAIL, single_line=True).lower()
    if not _EMAIL_RE.match(value):
        raise ValidationError(field, "must be a valid email address")
    return value


def validate_status(value: Optional[str]) -> str:
    value = (value or "").strip()
    allowed = [s.value for s in TicketStatus]
    if value not in allowed:
        raise ValidationError("status", f"must be one of: {', '.join(allowed)}")
    return value


def validate_category(value: Optional[str]) -> str:
    return clean_text(value, "category", MAX_CATEGORY, single_line=True)
