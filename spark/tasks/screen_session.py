"""Per-screen working memory (Jesse 2026-06-11).

"Everything for a screen should be stored and referenceable until the screen
advances." Each (platform, skeleton_hash) carries a SESSION: every attempt
and its outcome, measured facts (calibrations, topologies), lessons, and the
in-flight multi-step PLAN. The session is injected into every worker build
for that screen so attempts RESUME instead of re-deriving from zero — the
cross-cycle amnesia that thrashed the interactive-graph question is the
failure mode this kills.

Lifecycle:
  - session keyed by (platform, skel_hash); RESET when the question content
    fingerprint changes (collision-prone assessment pages reuse skeletons
    across questions — a new question is a new session).
  - record_attempt() on every executed-BT outcome; record_fact() for durable
    measurements; set_plan() for multi-step plan state.
  - archive() when the screen ADVANCES (validated) — raw history moves to
    an archive file; distilled lessons should be folded into knowledge.json
    operational_notes by claude-primary where generalizable.

Storage: /home/user/taey-ed-data/screen_sessions/{platform}/{skel_hash}.json
NO truncation anywhere (Jesse standing rule).
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

_BASE = Path("/home/user/taey-ed-data/screen_sessions")
_ALLOWED_AUTHORS = {"worker", "machine"}


def _path(platform: str, skel_hash: str) -> Path:
    d = _BASE / platform
    d.mkdir(parents=True, exist_ok=True)
    return d / f"{skel_hash}.json"


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _load(platform: str, skel_hash: str) -> dict:
    p = _path(platform, skel_hash)
    if p.exists():
        try:
            return json.loads(p.read_text())
        except (json.JSONDecodeError, OSError) as e:
            logger.warning(f"screen_session: unreadable {p}: {e} — starting fresh")
    return {
        "platform": platform,
        "skel_hash": skel_hash,
        "question_fingerprint": None,
        "started_at": _now(),
        "attempts": [],
        "facts": {},
        "plan": None,
        "lessons": [],
    }


def _save(platform: str, skel_hash: str, data: dict) -> None:
    from spark.tasks.atomic_write import atomic_write_json
    atomic_write_json(_path(platform, skel_hash), data)


def _require_author(author: str) -> str:
    author_name = str(author or "").strip()
    if author_name == "supervisor":
        raise ValueError("screen_session rejects author=supervisor; supervisor learning must go through YAML")
    if author_name not in _ALLOWED_AUTHORS:
        raise ValueError(f"screen_session rejected unknown author={author_name!r}")
    return author_name


def get_session(platform: str, skel_hash: str,
                fingerprint: Optional[dict] = None) -> dict:
    """Load the screen's session; reset it if the question content changed
    (same skeleton, different question — collision pages)."""
    data = _load(platform, skel_hash)
    if fingerprint is not None:
        fp = json.dumps(fingerprint, sort_keys=True)
        if data.get("question_fingerprint") is None:
            data["question_fingerprint"] = fp
            _save(platform, skel_hash, data)
        elif data["question_fingerprint"] != fp:
            archive(platform, skel_hash, reason="question_changed")
            data = _load(platform, skel_hash)
            data["question_fingerprint"] = fp
            _save(platform, skel_hash, data)
    return data


def record_attempt(platform: str, skel_hash: str, *,
                   bt_actions: Optional[list] = None,
                   outcome: str = "",
                   detail: str = "",
                   author: str) -> None:
    """Append an executed-BT outcome to the screen's history."""
    author_name = _require_author(author)
    data = _load(platform, skel_hash)
    data["attempts"].append({
        "at": _now(),
        "author": author_name,
        "bt_actions": bt_actions or [],
        "outcome": outcome,
        "detail": detail,
    })
    _save(platform, skel_hash, data)


def record_fact(platform: str, skel_hash: str, key: str, value, *, author: str) -> None:
    """Store a durable measured fact (calibration, topology, positions)."""
    author_name = _require_author(author)
    data = _load(platform, skel_hash)
    data["facts"][key] = {"value": value, "at": _now(), "author": author_name}
    _save(platform, skel_hash, data)


def set_plan(platform: str, skel_hash: str, plan, *, author: str) -> None:
    """Store/replace the in-flight multi-step plan (any JSON shape the worker
    chooses — typically {'steps': [...], 'done': [...], 'next': ...})."""
    author_name = _require_author(author)
    data = _load(platform, skel_hash)
    data["plan"] = {"value": plan, "at": _now(), "author": author_name}
    _save(platform, skel_hash, data)


def add_lesson(platform: str, skel_hash: str, lesson: str, *, author: str) -> None:
    author_name = _require_author(author)
    data = _load(platform, skel_hash)
    data["lessons"].append({"at": _now(), "lesson": lesson, "author": author_name})
    _save(platform, skel_hash, data)


def archive(platform: str, skel_hash: str, reason: str = "advanced") -> None:
    """Screen advanced (or question changed): move raw session to archive."""
    p = _path(platform, skel_hash)
    if not p.exists():
        return
    arch_dir = _BASE / platform / "archive"
    arch_dir.mkdir(parents=True, exist_ok=True)
    dest = arch_dir / f"{skel_hash}_{int(time.time())}_{reason}.json"
    try:
        p.rename(dest)
        logger.info(f"screen_session: archived {platform}/{skel_hash[:12]} ({reason})")
    except OSError as e:
        logger.warning(f"screen_session: archive failed for {p}: {e}")


def render_for_prompt(platform: str, skel_hash: str,
                      fingerprint: Optional[dict] = None) -> str:
    """Render the session as a prompt block. Empty string when no history.

    The worker MUST read this before planning: prior attempts on THIS screen,
    measured facts, and the standing plan. Resuming beats re-deriving."""
    data = get_session(platform, skel_hash, fingerprint)
    if not (data["attempts"] or data["facts"] or data["plan"] or data["lessons"]):
        return ""
    parts = ["=== THIS SCREEN'S SESSION (working memory — READ BEFORE PLANNING) ===",
             "Prior attempts on THIS exact screen/question, measured facts, and the",
             "standing plan. RESUME the plan and REUSE the facts — do not re-derive,",
             "do not repeat an approach that already failed below.", ""]
    if data["facts"]:
        parts.append("MEASURED FACTS (trust these — empirically verified on this screen):")
        for k, v in data["facts"].items():
            author = v.get("author", "unknown")
            parts.append(f"  - {k}: {json.dumps(v['value'])} (at {v['at']}, author={author})")
        parts.append("")
    if data["plan"]:
        plan_author = data["plan"].get("author", "unknown")
        parts.append(
            f"STANDING PLAN (set {data['plan']['at']} by {plan_author} — resume, don't restart):"
        )
        parts.append(f"  {json.dumps(data['plan']['value'], indent=2)}")
        parts.append("")
    if data["attempts"]:
        parts.append(f"ATTEMPT HISTORY ({len(data['attempts'])} so far):")
        for i, a in enumerate(data["attempts"], 1):
            acts = ",".join(a["bt_actions"]) if a["bt_actions"] else "?"
            author = a.get("author", "unknown")
            parts.append(f"  {i}. [{a['at']}] author={author} actions=[{acts}] -> {a['outcome']}"
                         + (f" | {a['detail']}" if a["detail"] else ""))
        parts.append("")
    if data["lessons"]:
        parts.append("LESSONS ON THIS SCREEN:")
        for l in data["lessons"]:
            author = l.get("author", "unknown")
            parts.append(f"  - {l['lesson']} (author={author})")
        parts.append("")
    parts.append("TO UPDATE THE SESSION: include a top-level \"_session\" object in your")
    parts.append("response JSON: {\"facts\": {...}, \"plan\": {...}, \"lesson\": \"...\"} —")
    parts.append("anything you measure or decide that the NEXT cycle must know.")
    return "\n".join(parts)


def absorb_worker_session(platform: str, skel_hash: str, response: dict) -> None:
    """Store the worker's _session contribution from its response JSON."""
    s = response.get("_session")
    if not isinstance(s, dict):
        return
    for k, v in (s.get("facts") or {}).items():
        record_fact(platform, skel_hash, k, v, author="worker")
    if s.get("plan") is not None:
        set_plan(platform, skel_hash, s["plan"], author="worker")
    if s.get("lesson"):
        add_lesson(platform, skel_hash, str(s["lesson"]), author="worker")
    logger.info(f"screen_session: absorbed worker _session for {platform}/{skel_hash[:12]}")
