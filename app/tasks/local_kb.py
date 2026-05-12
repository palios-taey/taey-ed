"""
Local KB — sqlite + numpy implementation.

Architectural decision (2026-05-12, surfaced to taey-ed in CCM dispatch
reply): the LAUNCH_PLAN §4 Gap E acceptance criteria require local
(text, vector) storage + top-K cosine retrieval + per-course delete.
DeepTutor (HKUDS/DeepTutor v1.3.10) is a full multi-user web tutoring
platform — too heavy to bundle and shaped for files-not-text. We use
the minimum primitives (sqlite + numpy + our embedding_client) to meet
the acceptance criteria. Public surface unchanged from the prior facade
so callers don't care.

Storage layout:
  ~/Library/Application Support/Taey-Ed/kb/<course_id>/
    chunks.db     sqlite: kb_chunk_id, source_screen_type, source_screen_id,
                          captured_at, text
    vectors.npy   numpy float32 array, shape (N, 4096); row order matches
                  sqlite primary-key order

Both files written atomically per add_document (database transaction +
np.save tmp+rename). Per-course delete = rm -rf the course directory.

Hard rule (per dispatch 2026-05-12): no truncation anywhere. Vectors stay
native 4096-dim. If embed returns a different size we raise.
"""

import hashlib
import json
import logging
import os
import shutil
import sqlite3
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import List, Optional

import numpy as np

from app.tasks.embedding_client import EMBEDDING_DIMENSION, embed, embed_one

logger = logging.getLogger("taey-ed")

KB_ROOT = Path.home() / "Library" / "Application Support" / "taey-ed" / "kb"


@dataclass
class KBChunk:
    """One chunk surfaced from the local KB. Shape mirrors spark/models.py
    KBChunk so the dict can be JSON-serialized into NextActionRequest.

    Fields:
      source_screen_type: "VIDEO" | "ARTICLE"
      source_screen_id:   stable hash of (course_id, screen_signature)
      captured_at:        ISO8601 timestamp when content was ingested
      text:               chunk text (no truncation)
      score:              float in [0, 1], cosine similarity to query
      kb_chunk_id:        local-only opaque ID (sha256 prefix)
    """
    source_screen_type: str
    source_screen_id: str
    captured_at: str
    text: str
    score: float
    kb_chunk_id: str

    def to_dict(self) -> dict:
        return asdict(self)


# ---------------------------------------------------------------------------
# ID helpers
# ---------------------------------------------------------------------------

def make_kb_chunk_id(course_id: str, source_screen_id: str, text: str) -> str:
    """Deterministic kb_chunk_id so duplicate captures dedup naturally."""
    h = hashlib.sha256()
    h.update(course_id.encode("utf-8")); h.update(b"\x1f")
    h.update(source_screen_id.encode("utf-8")); h.update(b"\x1f")
    h.update(text.encode("utf-8"))
    return h.hexdigest()[:16]


def make_source_screen_id(course_id: str, screen_signature: str) -> str:
    """Stable source_screen_id from course + screen signature."""
    h = hashlib.sha256()
    h.update(course_id.encode("utf-8")); h.update(b"\x1f")
    h.update(screen_signature.encode("utf-8"))
    return h.hexdigest()[:12]


# ---------------------------------------------------------------------------
# Storage internals
# ---------------------------------------------------------------------------

def _course_dir(course_id: str) -> Path:
    if not course_id:
        raise ValueError("course_id required")
    p = KB_ROOT / course_id
    p.mkdir(parents=True, exist_ok=True)
    return p


def _db_path(course_id: str) -> Path:
    return _course_dir(course_id) / "chunks.db"


def _vec_path(course_id: str) -> Path:
    return _course_dir(course_id) / "vectors.npy"


def _open_db(course_id: str) -> sqlite3.Connection:
    conn = sqlite3.connect(str(_db_path(course_id)))
    conn.execute("""
        CREATE TABLE IF NOT EXISTS chunks (
          row_id INTEGER PRIMARY KEY AUTOINCREMENT,
          kb_chunk_id TEXT UNIQUE NOT NULL,
          source_screen_type TEXT NOT NULL,
          source_screen_id TEXT NOT NULL,
          captured_at TEXT NOT NULL,
          text TEXT NOT NULL
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS chunks_kb_chunk_id_idx ON chunks(kb_chunk_id)")
    return conn


def _load_vectors(course_id: str) -> Optional[np.ndarray]:
    """Return the course's vector matrix, or None if not yet created."""
    p = _vec_path(course_id)
    if not p.exists():
        return None
    arr = np.load(str(p))
    if arr.ndim != 2 or arr.shape[1] != EMBEDDING_DIMENSION:
        raise RuntimeError(
            f"local_kb {course_id}: vectors.npy shape {arr.shape} doesn't match "
            f"EMBEDDING_DIMENSION={EMBEDDING_DIMENSION}. KB corrupted or stale."
        )
    return arr.astype(np.float32, copy=False)


def _save_vectors(course_id: str, arr: np.ndarray) -> None:
    """Atomic write of vectors.npy.

    np.save auto-appends `.npy` if the path doesn't already end in it, so the
    tmp filename must already end in `.npy` for the file to land at the path
    we then os.replace from."""
    p = _vec_path(course_id)
    tmp = p.parent / (p.stem + ".tmp.npy")
    np.save(str(tmp), arr.astype(np.float32, copy=False))
    os.replace(str(tmp), str(p))


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def is_enabled() -> bool:
    """True when the local KB is usable. Currently always True (sqlite + numpy
    are stdlib/dep-pinned in requirements.txt)."""
    return True


def add_document(
    course_id: str,
    text: str,
    source_screen_type: str,
    source_screen_id: str,
    captured_at: Optional[str] = None,
) -> Optional[str]:
    """Embed text via /api/v1/embed and append to the course's local KB.

    Idempotent on (course_id, source_screen_id, text) — calling twice with
    the same triple is a no-op (returns the same kb_chunk_id).

    Returns:
      kb_chunk_id on success (insert or existing).

    Raises:
      ValueError on bad input.
      EmbeddingError on embed failure.
    """
    if not text or not text.strip():
        raise ValueError("add_document: text cannot be empty/whitespace")
    if source_screen_type not in ("VIDEO", "ARTICLE"):
        raise ValueError(
            f"add_document: source_screen_type must be VIDEO or ARTICLE, got {source_screen_type!r}"
        )
    if captured_at is None:
        captured_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

    kb_chunk_id = make_kb_chunk_id(course_id, source_screen_id, text)

    conn = _open_db(course_id)
    try:
        # Dedup: if this exact triple is already stored, no-op.
        cur = conn.execute(
            "SELECT row_id FROM chunks WHERE kb_chunk_id = ?", (kb_chunk_id,),
        )
        if cur.fetchone() is not None:
            logger.info(f"local_kb add_document: dedup hit {kb_chunk_id} course={course_id}")
            return kb_chunk_id

        # Embed BEFORE inserting so we never end up with a row without a vector.
        vector = embed_one(text)
        if len(vector) != EMBEDDING_DIMENSION:
            raise RuntimeError(
                f"add_document: embed returned dim {len(vector)} != {EMBEDDING_DIMENSION}"
            )

        # Append vector to the course matrix, save atomically.
        existing = _load_vectors(course_id)
        new_row = np.asarray(vector, dtype=np.float32).reshape(1, -1)
        merged = new_row if existing is None else np.vstack([existing, new_row])
        _save_vectors(course_id, merged)

        # Insert the metadata row. The row_id must match merged's last index.
        # Using AUTOINCREMENT + immediate save above means we're safe — both
        # operations are atomic from this process and the file write happened
        # before the DB row, so if we crash between them the vector exists
        # without a metadata row (orphan vector — handled at query time by
        # row count).
        conn.execute(
            """INSERT INTO chunks
               (kb_chunk_id, source_screen_type, source_screen_id, captured_at, text)
               VALUES (?, ?, ?, ?, ?)""",
            (kb_chunk_id, source_screen_type, source_screen_id, captured_at, text),
        )
        conn.commit()
        logger.info(
            f"local_kb add_document: {course_id} {source_screen_type} {source_screen_id} "
            f"chunk={kb_chunk_id} dim={EMBEDDING_DIMENSION}"
        )
        return kb_chunk_id
    finally:
        conn.close()


def query(course_id: str, question_text: str, top_k: int = 5) -> List[KBChunk]:
    """Embed question, cosine-search the course's vectors, return top_k chunks.

    Returns empty list if the KB is empty or the course has no entries yet.
    """
    if not question_text or top_k < 1:
        return []

    conn = _open_db(course_id)
    try:
        rows = conn.execute(
            "SELECT row_id, kb_chunk_id, source_screen_type, source_screen_id, "
            "captured_at, text FROM chunks ORDER BY row_id ASC"
        ).fetchall()
    finally:
        conn.close()
    if not rows:
        return []

    vectors = _load_vectors(course_id)
    if vectors is None or vectors.shape[0] == 0:
        return []
    # Defensive: keep db and vectors aligned. If they got out of sync (rare
    # crash-between-write case), trim to the shorter one and warn.
    n = min(len(rows), vectors.shape[0])
    if n != len(rows) or n != vectors.shape[0]:
        logger.warning(
            f"local_kb {course_id}: row/vector mismatch ({len(rows)} vs {vectors.shape[0]}); "
            f"using first {n}"
        )

    q_vec = np.asarray(embed_one(question_text), dtype=np.float32).reshape(-1)

    # Cosine similarity. Normalize each side to unit length, take dot.
    mat = vectors[:n]
    mat_norms = np.linalg.norm(mat, axis=1, keepdims=True)
    mat_norms[mat_norms == 0] = 1.0
    mat_n = mat / mat_norms
    q_norm = np.linalg.norm(q_vec)
    if q_norm == 0:
        return []
    q_n = q_vec / q_norm
    sims = mat_n @ q_n  # shape (n,)

    # Top-K indices, descending by score.
    k = min(top_k, n)
    if k < n:
        top_idx = np.argpartition(-sims, k - 1)[:k]
    else:
        top_idx = np.arange(n)
    top_idx = top_idx[np.argsort(-sims[top_idx])]

    out: List[KBChunk] = []
    for i in top_idx:
        i = int(i)
        _row_id, kb_chunk_id, sst, ssi, cap_at, text = rows[i]
        out.append(KBChunk(
            source_screen_type=sst,
            source_screen_id=ssi,
            captured_at=cap_at,
            text=text,
            score=float(sims[i]),
            kb_chunk_id=kb_chunk_id,
        ))
    return out


def delete_course(course_id: str) -> bool:
    """Remove a course's entire local KB. Idempotent."""
    p = _course_dir(course_id)
    if not p.exists():
        return False
    shutil.rmtree(p)
    logger.info(f"local_kb: deleted course {course_id}")
    return True


def list_courses() -> List[str]:
    """Return course_ids that currently have a local KB directory."""
    if not KB_ROOT.exists():
        return []
    return sorted(p.name for p in KB_ROOT.iterdir() if p.is_dir() and not p.name.startswith("."))


def status(course_id: Optional[str] = None) -> dict:
    """Diagnostic snapshot."""
    if course_id is None:
        return {
            "enabled": is_enabled(),
            "kb_root": str(KB_ROOT),
            "courses": list_courses(),
            "embedding_dimension": EMBEDDING_DIMENSION,
        }
    p = _course_dir(course_id)
    db = _db_path(course_id)
    vec = _vec_path(course_id)
    chunk_count = 0
    if db.exists():
        conn = sqlite3.connect(str(db))
        try:
            cur = conn.execute("SELECT COUNT(*) FROM chunks")
            chunk_count = cur.fetchone()[0]
        finally:
            conn.close()
    return {
        "enabled": is_enabled(),
        "course_id": course_id,
        "path": str(p),
        "chunk_count": chunk_count,
        "db_exists": db.exists(),
        "vectors_exists": vec.exists(),
    }
