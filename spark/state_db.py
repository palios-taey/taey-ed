"""SQLite connection layer for the taey-ed state store.

The state store is separate from the auth/billing database. This module owns
schema loading, required PRAGMA enforcement, and BEGIN IMMEDIATE transaction
handling; state transitions live in spark.state_repo.
"""

from __future__ import annotations

import logging
import os
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from spark.tasks.paths import DATA_DIR

logger = logging.getLogger(__name__)

SCHEMA_PATH = Path(__file__).with_name("state_schema.sql")
BUSY_TIMEOUT_MS = 5000
BEGIN_RETRY_LIMIT = 5
BEGIN_RETRY_BASE_DELAY = 0.05


class StateStoreBusyError(RuntimeError):
    """Raised when a state-store write transaction cannot acquire the writer lock."""


def now_ms() -> int:
    return int(time.time() * 1000)


def state_db_path() -> Path:
    raw = os.environ.get("TAEY_ED_STATE_DB_PATH")
    if raw:
        return Path(raw).expanduser()
    return DATA_DIR / "taey_state.db"


def _connect(db_path: Path | None = None) -> sqlite3.Connection:
    path = db_path or state_db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(
        str(path),
        timeout=BUSY_TIMEOUT_MS / 1000,
        isolation_level=None,
    )
    conn.row_factory = sqlite3.Row
    _apply_and_assert_pragmas(conn)
    return conn


def _apply_and_assert_pragmas(conn: sqlite3.Connection) -> None:
    journal_mode = conn.execute("PRAGMA journal_mode=WAL").fetchone()[0]
    if str(journal_mode).lower() != "wal":
        raise RuntimeError(f"state DB journal_mode must be WAL, got {journal_mode!r}")

    conn.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA recursive_triggers=ON")

    assertions = {
        "busy_timeout": (BUSY_TIMEOUT_MS,),
        "synchronous": (1,),
        "foreign_keys": (1,),
        "recursive_triggers": (1,),
    }
    for pragma, expected in assertions.items():
        value = conn.execute(f"PRAGMA {pragma}").fetchone()[0]
        if value not in expected:
            raise RuntimeError(f"state DB PRAGMA {pragma} expected {expected}, got {value!r}")


def init_state_db(db_path: Path | None = None) -> Path:
    path = db_path or state_db_path()
    if not SCHEMA_PATH.exists():
        raise RuntimeError(f"state schema missing at {SCHEMA_PATH}")
    with _connect(path) as conn:
        conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
    logger.info("State DB initialized at %s", path)
    return path


@contextmanager
def state_connection(db_path: Path | None = None) -> Iterator[sqlite3.Connection]:
    conn = _connect(db_path)
    try:
        yield conn
    finally:
        conn.close()


@contextmanager
def immediate_transaction(db_path: Path | None = None) -> Iterator[sqlite3.Connection]:
    conn = _connect(db_path)
    try:
        _begin_immediate(conn)
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _begin_immediate(conn: sqlite3.Connection) -> None:
    delay = BEGIN_RETRY_BASE_DELAY
    for attempt in range(1, BEGIN_RETRY_LIMIT + 1):
        try:
            conn.execute("BEGIN IMMEDIATE")
            return
        except sqlite3.OperationalError as exc:
            if not _is_busy(exc) or attempt == BEGIN_RETRY_LIMIT:
                if _is_busy(exc):
                    raise StateStoreBusyError("state DB writer lock busy after retries") from exc
                raise
            logger.warning(
                "state DB BEGIN IMMEDIATE busy on attempt %s/%s; retrying",
                attempt,
                BEGIN_RETRY_LIMIT,
            )
            time.sleep(delay)
            delay *= 2


def _is_busy(exc: sqlite3.OperationalError) -> bool:
    text = str(exc).lower()
    return "database is locked" in text or "database is busy" in text
