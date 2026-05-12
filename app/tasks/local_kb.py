"""
Local KB facade — DeepTutor integration on the user's Mac.

Hard architectural line (LAUNCH_PLAN §1 + dispatch 2026-05-12):
  DeepTutor and the user's full KB (content + vectors) live on the user's
  Mac. Mira does embedding generation only. The KB never leaves the Mac.
  Only the top-K retrieved chunks travel with one consultation request.

This module is the ONLY place app code touches DeepTutor. Everything goes
through these four functions:

  add_document(course_id, text, source_screen_type, source_screen_id) -> kb_chunk_id
  query(course_id, question_text, top_k=5) -> List[KBChunk]
  delete_course(course_id) -> bool
  status(course_id=None) -> dict

DeepTutor (https://github.com/HKUDS/DeepTutor) is not yet bundled. The
bundling step (Task 20 in the launch plan) drops the package under the
.app's Resources/ and templates its .env to point at the central embed
service. Until then, these functions raise NotImplementedError. The
content-capture / retrieval call sites should be wired against this
facade regardless — when bundling lands, only this file changes.

Local KB storage path: ~/Library/Application Support/Taey-Ed/deeptutor/
(one subdirectory per course_id). Per-course delete = rm -rf that subdir.
"""

import hashlib
import logging
import os
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import List, Optional

logger = logging.getLogger("taey-ed")

KB_ROOT = Path.home() / "Library" / "Application Support" / "Taey-Ed" / "deeptutor"


@dataclass
class KBChunk:
    """One chunk surfaced from the local KB. Shape mirrors spark/models.py
    KBChunk so the dict can be JSON-serialized into NextActionRequest.

    Fields:
      source_screen_type: "VIDEO" | "ARTICLE"
      source_screen_id:   stable hash of (course_id, screen_signature)
      captured_at:        ISO8601 timestamp when content was ingested
      text:               chunk text (no truncation; full chunk as DeepTutor returns it)
      score:              float in [0, 1], cosine similarity to query
      kb_chunk_id:        local-only opaque ID (UUID or hash)
    """
    source_screen_type: str
    source_screen_id: str
    captured_at: str
    text: str
    score: float
    kb_chunk_id: str

    def to_dict(self) -> dict:
        return asdict(self)


def _course_dir(course_id: str) -> Path:
    """Path to a course's KB directory. Creates parent if missing."""
    if not course_id:
        raise ValueError("course_id required")
    p = KB_ROOT / course_id
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def make_kb_chunk_id(course_id: str, source_screen_id: str, text: str) -> str:
    """Deterministic kb_chunk_id so duplicate captures dedup naturally.
    Uses sha256 of (course_id, source_screen_id, text) — first 16 hex chars.
    Stable across re-runs; lets DeepTutor's ingest dedup before re-embedding."""
    h = hashlib.sha256()
    h.update(course_id.encode("utf-8"))
    h.update(b"\x1f")
    h.update(source_screen_id.encode("utf-8"))
    h.update(b"\x1f")
    h.update(text.encode("utf-8"))
    return h.hexdigest()[:16]


def make_source_screen_id(course_id: str, screen_signature: str) -> str:
    """Stable source_screen_id from course + screen signature.
    Used as the back-pointer so the model can cite which lesson the chunk
    came from."""
    h = hashlib.sha256()
    h.update(course_id.encode("utf-8"))
    h.update(b"\x1f")
    h.update(screen_signature.encode("utf-8"))
    return h.hexdigest()[:12]


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def is_enabled() -> bool:
    """True when DeepTutor is bundled and available."""
    # Filled in when DeepTutor bundling lands (Task 20).
    # For now: presence of the directory + a marker file controls the gate so
    # the rest of the code can read this flag safely without raising.
    return (KB_ROOT / ".bundled").exists()


def add_document(
    course_id: str,
    text: str,
    source_screen_type: str,
    source_screen_id: str,
    captured_at: Optional[str] = None,
) -> Optional[str]:
    """Ingest a captured page (transcript / article body) into the course's
    local KB. Returns the kb_chunk_id, or None if a no-op (e.g. dedup hit).

    Raises:
      NotImplementedError: until DeepTutor bundling lands.
      ValueError: bad input.
    """
    if not text:
        raise ValueError("add_document: text cannot be empty")
    if source_screen_type not in ("VIDEO", "ARTICLE"):
        raise ValueError(f"add_document: source_screen_type must be VIDEO or ARTICLE, got {source_screen_type!r}")

    if not is_enabled():
        # Surface clearly — content-capture path will log and continue.
        raise NotImplementedError(
            "Local KB (DeepTutor) is not yet bundled. add_document is a no-op."
        )

    # TODO(Task 20): hand `text` to DeepTutor's ingest API for this course.
    # DeepTutor internally chunks, calls embedding_client.embed(...), and
    # stores (text, vector, metadata) in its FAISS index for the course.
    # We return the deterministic kb_chunk_id so callers can confirm.
    raise NotImplementedError("DeepTutor integration pending Task 20.")


def query(course_id: str, question_text: str, top_k: int = 5) -> List[KBChunk]:
    """Look up top_k relevant chunks for the question from the course's KB.

    Returns:
      List of KBChunk, length ≤ top_k. Empty list if KB is empty or disabled.
    """
    if not question_text:
        return []
    if top_k < 1:
        return []

    if not is_enabled():
        # Soft fallback: empty list rather than raise — caller can still
        # proceed with the consultation, just without retrieved context.
        return []

    # TODO(Task 20): query DeepTutor's per-course FAISS index with the
    # question_text. DeepTutor embeds the query (via central /api/v1/embed),
    # cosine-searches, returns top_k hits with scores. We adapt each hit
    # into a KBChunk dataclass.
    return []


def delete_course(course_id: str) -> bool:
    """Remove a course's entire local KB. Idempotent. Returns True if a
    directory existed and was removed."""
    p = _course_dir(course_id)
    if not p.exists():
        return False
    import shutil
    shutil.rmtree(p)
    logger.info(f"local_kb: deleted course {course_id}")
    return True


def list_courses() -> List[str]:
    """Return course_ids that currently have a local KB."""
    if not KB_ROOT.exists():
        return []
    return sorted(p.name for p in KB_ROOT.iterdir() if p.is_dir() and not p.name.startswith("."))


def status(course_id: Optional[str] = None) -> dict:
    """Diagnostic snapshot. With course_id: that course's stats. Without:
    overall enablement + course list."""
    if course_id is None:
        return {
            "enabled": is_enabled(),
            "kb_root": str(KB_ROOT),
            "courses": list_courses(),
        }
    p = _course_dir(course_id)
    return {
        "enabled": is_enabled(),
        "course_id": course_id,
        "path": str(p),
        "exists": p.exists(),
    }
