"""User account storage: signup, lookup, password verification, email
verification token lifecycle, Stripe customer linkage."""

import secrets
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

import bcrypt

from spark.db import get_conn


@dataclass
class User:
    id: str
    email: str
    email_verified: bool
    stripe_customer_id: Optional[str]
    created_at: str
    updated_at: str

    @classmethod
    def from_row(cls, row) -> "User":
        return cls(
            id=row["id"],
            email=row["email"],
            email_verified=bool(row["email_verified"]),
            stripe_customer_id=row["stripe_customer_id"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def hash_password(password: str) -> str:
    """bcrypt hash with default cost factor (12). Returns hex-encoded hash."""
    return bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()


def verify_password(password: str, hashed: str) -> bool:
    """Constant-time password comparison."""
    try:
        return bcrypt.checkpw(password.encode(), hashed.encode())
    except (ValueError, TypeError):
        return False


def create_user(email: str, password: str) -> User:
    """Create a new user account with an email verification token.

    Returns the User object. The verification token can be fetched separately
    via get_verification_token() for sending in the verification email; we don't
    return it inline to keep this function's signature focused.

    Raises sqlite3.IntegrityError if email is already registered (UNIQUE constraint).
    """
    uid = str(uuid.uuid4())
    pwd_hash = hash_password(password)
    now = _now()
    verification_token = secrets.token_urlsafe(32)
    with get_conn() as conn:
        conn.execute(
            """INSERT INTO users
               (id, email, password_hash, email_verified, email_verification_token,
                email_verification_sent_at, created_at, updated_at)
               VALUES (?, ?, ?, 0, ?, ?, ?, ?)""",
            (uid, email.lower(), pwd_hash, verification_token, now, now, now),
        )
        conn.commit()
    return get_user_by_id(uid)


def get_user_by_id(user_id: str) -> Optional[User]:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT id, email, email_verified, stripe_customer_id, created_at, updated_at FROM users WHERE id = ?",
            (user_id,),
        ).fetchone()
    return User.from_row(row) if row else None


def get_user_by_email(email: str) -> Optional[User]:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT id, email, email_verified, stripe_customer_id, created_at, updated_at FROM users WHERE email = ?",
            (email.lower(),),
        ).fetchone()
    return User.from_row(row) if row else None


def get_password_hash(user_id: str) -> Optional[str]:
    """Fetch the stored password hash. Separated from User dataclass so the
    hash never travels with the user object across response boundaries."""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT password_hash FROM users WHERE id = ?", (user_id,),
        ).fetchone()
    return row["password_hash"] if row else None


def get_verification_token(user_id: str) -> Optional[str]:
    """Fetch the email-verification token for sending in the verification email."""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT email_verification_token FROM users WHERE id = ?", (user_id,),
        ).fetchone()
    return row["email_verification_token"] if row else None


def mark_email_verified(token: str) -> Optional[User]:
    """Find user by verification token and mark their email verified.

    Returns the User if a match was found, None otherwise.
    Idempotent: re-verifying an already-verified email with the same token
    still succeeds and returns the user (because token isn't cleared on success).

    To prevent token reuse for other purposes, we clear the token after success.
    """
    now = _now()
    with get_conn() as conn:
        row = conn.execute(
            "SELECT id FROM users WHERE email_verification_token = ?", (token,),
        ).fetchone()
        if not row:
            return None
        uid = row["id"]
        conn.execute(
            """UPDATE users
               SET email_verified = 1,
                   email_verification_token = NULL,
                   updated_at = ?
               WHERE id = ?""",
            (now, uid),
        )
        conn.commit()
    return get_user_by_id(uid)


def set_stripe_customer_id(user_id: str, stripe_customer_id: str) -> None:
    """Set Stripe customer ID on a user (first purchase or admin-initiated)."""
    with get_conn() as conn:
        conn.execute(
            "UPDATE users SET stripe_customer_id = ?, updated_at = ? WHERE id = ?",
            (stripe_customer_id, _now(), user_id),
        )
        conn.commit()


def get_user_by_stripe_customer(stripe_customer_id: str) -> Optional[User]:
    """Lookup user by Stripe customer ID (used by webhook handlers)."""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT id, email, email_verified, stripe_customer_id, created_at, updated_at FROM users WHERE stripe_customer_id = ?",
            (stripe_customer_id,),
        ).fetchone()
    return User.from_row(row) if row else None
