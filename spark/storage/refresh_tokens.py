"""Refresh token storage. Refresh tokens are randomly-generated opaque strings;
we store their SHA-256 hashes (never raw) so a database leak doesn't expose
live tokens.

Tokens rotate on every use (the old token is revoked, a new one is issued).
This bounds the damage if a refresh token is stolen: the legitimate user's
next refresh invalidates the stolen token, and we log the anomaly.
"""

import hashlib
import secrets
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional

from spark.db import get_conn

# Refresh tokens live for 30 days. Access tokens (handled elsewhere) live for 15 min.
REFRESH_TOKEN_TTL_DAYS = 30


@dataclass
class RefreshTokenRecord:
    id: str
    user_id: str
    expires_at: str
    revoked_at: Optional[str]
    created_at: str
    last_used_at: Optional[str]


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _hash(raw_token: str) -> str:
    """SHA-256 of the raw refresh token. Used for storage and lookup."""
    return hashlib.sha256(raw_token.encode()).hexdigest()


def issue_refresh_token(
    user_id: str,
    user_agent: Optional[str] = None,
    ip_addr: Optional[str] = None,
) -> tuple[str, RefreshTokenRecord]:
    """Issue a new refresh token. Returns (raw_token, record).

    The raw token is sent to the client; only its hash is stored. The caller
    must send the raw token back to the server on refresh; the server hashes
    it and looks up the record.
    """
    raw = secrets.token_urlsafe(48)  # ~64 chars URL-safe
    token_hash = _hash(raw)
    record_id = str(uuid.uuid4())
    now = _now()
    expires_at = (now + timedelta(days=REFRESH_TOKEN_TTL_DAYS)).isoformat()

    with get_conn() as conn:
        conn.execute(
            """INSERT INTO refresh_tokens
               (id, user_id, token_hash, expires_at, created_at, user_agent, ip_addr)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (record_id, user_id, token_hash, expires_at, now.isoformat(),
             user_agent, ip_addr),
        )
        conn.commit()
        row = conn.execute(
            "SELECT * FROM refresh_tokens WHERE id = ?", (record_id,),
        ).fetchone()
    return raw, RefreshTokenRecord(
        id=row["id"],
        user_id=row["user_id"],
        expires_at=row["expires_at"],
        revoked_at=row["revoked_at"],
        created_at=row["created_at"],
        last_used_at=row["last_used_at"],
    )


def lookup_active(raw_token: str) -> Optional[RefreshTokenRecord]:
    """Look up an active (non-revoked, non-expired) refresh token by its raw
    value. Returns None if not found or no longer valid.
    """
    token_hash = _hash(raw_token)
    now_iso = _now().isoformat()
    with get_conn() as conn:
        row = conn.execute(
            """SELECT * FROM refresh_tokens
               WHERE token_hash = ?
                 AND revoked_at IS NULL
                 AND expires_at > ?""",
            (token_hash, now_iso),
        ).fetchone()
    if not row:
        return None
    return RefreshTokenRecord(
        id=row["id"],
        user_id=row["user_id"],
        expires_at=row["expires_at"],
        revoked_at=row["revoked_at"],
        created_at=row["created_at"],
        last_used_at=row["last_used_at"],
    )


def revoke(record_id: str) -> None:
    """Revoke a refresh token by ID. Used on rotate (after issuing replacement)
    and on logout (when user explicitly signs out).
    """
    with get_conn() as conn:
        conn.execute(
            "UPDATE refresh_tokens SET revoked_at = ? WHERE id = ? AND revoked_at IS NULL",
            (_now().isoformat(), record_id),
        )
        conn.commit()


def revoke_all_for_user(user_id: str) -> int:
    """Revoke every active refresh token for a user. Used for password reset
    or admin-initiated force-logout. Returns count revoked."""
    with get_conn() as conn:
        cur = conn.execute(
            "UPDATE refresh_tokens SET revoked_at = ? WHERE user_id = ? AND revoked_at IS NULL",
            (_now().isoformat(), user_id),
        )
        conn.commit()
        return cur.rowcount


def mark_used(record_id: str) -> None:
    """Update last_used_at when a refresh succeeds. Not strictly needed for
    auth, but useful for telemetry / detecting suspicious activity."""
    with get_conn() as conn:
        conn.execute(
            "UPDATE refresh_tokens SET last_used_at = ? WHERE id = ?",
            (_now().isoformat(), record_id),
        )
        conn.commit()
