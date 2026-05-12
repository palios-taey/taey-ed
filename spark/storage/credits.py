"""Append-only credit ledger.

Every change to a user's balance is a new ledger row. Reading balance is a
single indexed query for the latest row. Idempotency keys protect against
duplicate webhooks / retries / double-debits.

Billable event policy:
  - debit on successful screen completion (not on consultation creation)
  - retries / waits / video-poll / wrong-answer escalations do NOT debit
  - per LAUNCH_PLAN.md §4 Gap C

Per ChatGPT review:
  "credits buy completed screens, not attempts"
"""

import json
import logging
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

from spark.db import get_conn

logger = logging.getLogger(__name__)


@dataclass
class LedgerEntry:
    id: str
    user_id: str
    type: str  # purchase | debit | refund | adjustment | hold | release
    amount: int
    balance_after: int
    idempotency_key: str
    source: Optional[str]
    metadata: dict
    created_at: str

    @classmethod
    def from_row(cls, row) -> "LedgerEntry":
        return cls(
            id=row["id"],
            user_id=row["user_id"],
            type=row["type"],
            amount=row["amount"],
            balance_after=row["balance_after"],
            idempotency_key=row["idempotency_key"],
            source=row["source"],
            metadata=json.loads(row["metadata_json"]) if row["metadata_json"] else {},
            created_at=row["created_at"],
        )


class InsufficientCreditsError(RuntimeError):
    """Raised when a debit would take the balance below zero. The caller
    should surface this as HTTP 402 Payment Required."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _delta(entry_type: str, amount: int) -> int:
    """Convert (type, amount) into a signed balance delta.

    Positive: purchase, refund, adjustment (admin grant), release (held → free).
    Negative: debit, hold (free → held).
    """
    if entry_type in ("purchase", "refund", "adjustment", "release"):
        return amount
    if entry_type in ("debit", "hold"):
        return -amount
    raise ValueError(f"Unknown ledger type: {entry_type}")


def get_balance(user_id: str) -> int:
    """Current balance for a user. Returns 0 if no ledger rows exist yet."""
    with get_conn() as conn:
        row = conn.execute(
            """SELECT balance_after FROM credit_ledger
               WHERE user_id = ?
               ORDER BY created_at DESC, id DESC
               LIMIT 1""",
            (user_id,),
        ).fetchone()
    return row["balance_after"] if row else 0


def get_history(user_id: str, limit: int = 100) -> list[LedgerEntry]:
    """Recent ledger entries for a user, newest first."""
    with get_conn() as conn:
        rows = conn.execute(
            """SELECT * FROM credit_ledger
               WHERE user_id = ?
               ORDER BY created_at DESC, id DESC
               LIMIT ?""",
            (user_id, limit),
        ).fetchall()
    return [LedgerEntry.from_row(r) for r in rows]


def _append(
    user_id: str,
    entry_type: str,
    amount: int,
    idempotency_key: str,
    source: Optional[str] = None,
    metadata: Optional[dict] = None,
) -> LedgerEntry:
    """Internal: append a ledger row atomically.

    If a row already exists with this idempotency_key, returns the existing
    row instead of inserting (duplicate-suppression for retries / webhooks).

    Raises InsufficientCreditsError if the resulting balance would be negative.
    """
    if amount < 0:
        raise ValueError("amount must be non-negative; sign is implied by type")
    metadata_json = json.dumps(metadata) if metadata else None
    now = _now()
    delta = _delta(entry_type, amount)
    with get_conn() as conn:
        # Idempotency check: existing row with this key wins
        existing = conn.execute(
            "SELECT * FROM credit_ledger WHERE idempotency_key = ?",
            (idempotency_key,),
        ).fetchone()
        if existing:
            logger.info(
                f"credit_ledger: idempotency hit for key {idempotency_key[:20]}... "
                f"(returning existing balance_after={existing['balance_after']})"
            )
            return LedgerEntry.from_row(existing)

        # Compute new balance from latest row for this user
        latest = conn.execute(
            """SELECT balance_after FROM credit_ledger
               WHERE user_id = ?
               ORDER BY created_at DESC, id DESC
               LIMIT 1""",
            (user_id,),
        ).fetchone()
        prev_balance = latest["balance_after"] if latest else 0
        new_balance = prev_balance + delta

        if new_balance < 0:
            raise InsufficientCreditsError(
                f"User {user_id} balance would go negative "
                f"(current={prev_balance}, attempted_delta={delta})"
            )

        row_id = str(uuid.uuid4())
        try:
            conn.execute(
                """INSERT INTO credit_ledger
                   (id, user_id, type, amount, balance_after, idempotency_key,
                    source, metadata_json, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (row_id, user_id, entry_type, amount, new_balance,
                 idempotency_key, source, metadata_json, now),
            )
            conn.commit()
        except sqlite3.IntegrityError:
            # Race: another concurrent insert with same idempotency_key won.
            # Fetch and return the winner.
            existing = conn.execute(
                "SELECT * FROM credit_ledger WHERE idempotency_key = ?",
                (idempotency_key,),
            ).fetchone()
            if existing:
                return LedgerEntry.from_row(existing)
            raise

        inserted = conn.execute(
            "SELECT * FROM credit_ledger WHERE id = ?", (row_id,),
        ).fetchone()
    logger.info(
        f"credit_ledger: {entry_type} {amount} for user {user_id} "
        f"→ balance {new_balance} (source={source})"
    )
    return LedgerEntry.from_row(inserted)


# ── Public operations ──

def add_purchase(
    user_id: str,
    credits: int,
    stripe_session_id: str,
    metadata: Optional[dict] = None,
) -> LedgerEntry:
    """Credit a user's account after successful Stripe checkout.

    Idempotency key uses stripe_session_id so a duplicate webhook is a no-op.
    """
    return _append(
        user_id=user_id,
        entry_type="purchase",
        amount=credits,
        idempotency_key=f"purchase:{stripe_session_id}",
        source=stripe_session_id,
        metadata=metadata,
    )


def debit_screen(
    user_id: str,
    directive_id: str,
    screen_hash_before: str,
    screen_hash_after: str,
    credits: int = 1,
    metadata: Optional[dict] = None,
) -> LedgerEntry:
    """Debit on successful screen completion (one credit per screen).

    Idempotency key combines directive + screen hashes so re-applying the same
    completion event is a no-op. Per LAUNCH_PLAN.md: only debit on completion,
    never on retries / waits / failed attempts.

    Raises InsufficientCreditsError if balance would go negative.
    """
    key = f"debit:{user_id}:{directive_id}:{screen_hash_before}:{screen_hash_after}"
    return _append(
        user_id=user_id,
        entry_type="debit",
        amount=credits,
        idempotency_key=key,
        source=directive_id,
        metadata=metadata,
    )


def add_refund(
    user_id: str,
    credits: int,
    reason: str,
    admin_id: str,
    metadata: Optional[dict] = None,
) -> LedgerEntry:
    """Manual refund (support tool). Admin-only."""
    md = {"reason": reason, **(metadata or {})}
    key = f"refund:{user_id}:{admin_id}:{datetime.now(timezone.utc).timestamp()}"
    return _append(
        user_id=user_id,
        entry_type="refund",
        amount=credits,
        idempotency_key=key,
        source=f"manual:{admin_id}",
        metadata=md,
    )


def add_adjustment(
    user_id: str,
    credits: int,
    reason: str,
    admin_id: str,
) -> LedgerEntry:
    """Manual credit grant (welcome bonus, support apology, etc.)."""
    key = f"adjustment:{user_id}:{admin_id}:{datetime.now(timezone.utc).timestamp()}"
    return _append(
        user_id=user_id,
        entry_type="adjustment",
        amount=credits,
        idempotency_key=key,
        source=f"manual:{admin_id}",
        metadata={"reason": reason},
    )
