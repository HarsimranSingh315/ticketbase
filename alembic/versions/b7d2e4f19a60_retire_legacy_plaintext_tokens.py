"""retire legacy plaintext session and invite tokens

Revision ID: b7d2e4f19a60
Revises: e38103fdb554
Create Date: 2026-09-25

Session IDs and invite tokens have been stored as SHA-256 hashes since
commit 4dec779. Rows created BEFORE that still hold the raw bearer
secret. Lookups now hash the incoming value, so those rows no longer
authenticate - but they remain readable secrets at rest, which is the
exact exposure hashing was meant to remove.

Distinguishing the two is unambiguous: a hash is exactly 64 lowercase
hex characters; a legacy token is secrets.token_urlsafe(32) output
(43 characters of [A-Za-z0-9_-]), which can never match that pattern.
That also makes this migration idempotent.

Two deliberately different treatments:
- Legacy SESSIONS are deleted. Affected users simply log in again; that
  costs little, and nothing forgotten about a stale session can linger.
- Legacy INVITES are hashed in place. The admin already sent those links
  to real people; hashing the stored token makes the ORIGINAL link work
  again under the new lookup, instead of forcing every pending invite
  to be reissued by hand. Used/expired invites are hashed too - they
  can't be redeemed either way, but they shouldn't hold raw secrets.

Downgrade is intentionally a no-op: a hash cannot be reversed, and
restoring plaintext secrets would be the wrong direction anyway.
"""
import hashlib
import re

import sqlalchemy as sa
from alembic import op

revision = "b7d2e4f19a60"
down_revision = "e38103fdb554"
branch_labels = None
depends_on = None

_HASHED = re.compile(r"^[0-9a-f]{64}$")


def upgrade() -> None:
    conn = op.get_bind()

    session_ids = [row[0] for row in conn.execute(sa.text("SELECT id FROM sessions"))]
    legacy_sessions = [sid for sid in session_ids if not _HASHED.match(sid or "")]
    for sid in legacy_sessions:
        conn.execute(sa.text("DELETE FROM sessions WHERE id = :id"), {"id": sid})

    invites = list(conn.execute(sa.text("SELECT id, token FROM invites")))
    legacy_invites = [(iid, tok) for iid, tok in invites if not _HASHED.match(tok or "")]
    for iid, tok in legacy_invites:
        hashed = hashlib.sha256(tok.encode("utf-8")).hexdigest()
        conn.execute(sa.text("UPDATE invites SET token = :t WHERE id = :id"), {"t": hashed, "id": iid})

    print(f"retire_legacy_plaintext_tokens: deleted {len(legacy_sessions)} legacy session(s), "
          f"hashed {len(legacy_invites)} legacy invite token(s)")


def downgrade() -> None:
    pass
