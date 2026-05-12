"""SQLite database for Taey-Ed central server (users, refresh tokens, credit ledger).

Single file at DATA_DIR/db/taey_ed.db. SQLite is sufficient for MVP friends-and-
family scale. Migration to Postgres is a one-day swap when concurrent writes
or multi-host become real concerns.

Schema is initialized idempotently at server startup via init_db(). The ledger
is APPEND-ONLY (no UPDATE or DELETE on credit_ledger rows after insert).
"""

import logging
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path

from spark.tasks.paths import DATA_DIR

logger = logging.getLogger(__name__)

DB_PATH = DATA_DIR / "db" / "taey_ed.db"

# SQLite connections are NOT thread-safe by default; wrap in a lock for our
# single-writer pattern. FastAPI handles requests on a thread pool so concurrent
# writes are possible without serialization.
_lock = threading.Lock()


SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id TEXT PRIMARY KEY,
    email TEXT UNIQUE NOT NULL COLLATE NOCASE,
    password_hash TEXT NOT NULL,
    email_verified INTEGER NOT NULL DEFAULT 0,
    email_verification_token TEXT,
    email_verification_sent_at TEXT,
    stripe_customer_id TEXT UNIQUE,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_users_email ON users(email);
CREATE INDEX IF NOT EXISTS idx_users_stripe ON users(stripe_customer_id);
CREATE INDEX IF NOT EXISTS idx_users_verification ON users(email_verification_token);

CREATE TABLE IF NOT EXISTS refresh_tokens (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    token_hash TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    revoked_at TEXT,
    created_at TEXT NOT NULL,
    last_used_at TEXT,
    user_agent TEXT,
    ip_addr TEXT,
    FOREIGN KEY (user_id) REFERENCES users(id)
);

CREATE INDEX IF NOT EXISTS idx_refresh_tokens_user ON refresh_tokens(user_id);
CREATE INDEX IF NOT EXISTS idx_refresh_tokens_hash ON refresh_tokens(token_hash);
CREATE INDEX IF NOT EXISTS idx_refresh_tokens_active
    ON refresh_tokens(user_id, revoked_at, expires_at);

-- Append-only credit ledger. Every change to a user's balance is a new row.
-- balance_after is computed at insert time inside a transaction; the latest
-- row for a user is their current balance.
-- type: purchase | debit | refund | adjustment | hold | release
-- amount: always non-negative; sign is implied by type
-- idempotency_key: globally unique; duplicate insert is a no-op
CREATE TABLE IF NOT EXISTS credit_ledger (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    type TEXT NOT NULL CHECK (type IN ('purchase', 'debit', 'refund', 'adjustment', 'hold', 'release')),
    amount INTEGER NOT NULL CHECK (amount >= 0),
    balance_after INTEGER NOT NULL,
    idempotency_key TEXT UNIQUE NOT NULL,
    source TEXT,
    metadata_json TEXT,
    created_at TEXT NOT NULL,
    FOREIGN KEY (user_id) REFERENCES users(id)
);

CREATE INDEX IF NOT EXISTS idx_ledger_user ON credit_ledger(user_id);
CREATE INDEX IF NOT EXISTS idx_ledger_user_created ON credit_ledger(user_id, created_at DESC);
"""


def init_db() -> None:
    """Create the database file + schema if missing. Idempotent. Call once at
    server startup."""
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(str(DB_PATH), timeout=10) as conn:
        conn.executescript(SCHEMA)
        conn.commit()
    logger.info(f"DB initialized at {DB_PATH}")


@contextmanager
def get_conn():
    """Context manager for a SQLite connection with FK enforcement enabled
    and a per-request lock to serialize writes from the FastAPI threadpool.

    Reads are also serialized; for our MVP write volume this is fine. If
    contention becomes visible, switch to WAL mode + per-request read conn.
    """
    conn = sqlite3.connect(str(DB_PATH), timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        with _lock:
            yield conn
    finally:
        conn.close()
