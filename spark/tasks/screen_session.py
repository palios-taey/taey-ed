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
_MAX_LIVE_ATTEMPTS = 6
_MAX_LIVE_LESSONS = 6
_SESSION_RENDER_CHAR_BUDGET = 9_000


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


def _archive_log_path(platform: str, skel_hash: str) -> Path:
    d = _BASE / platform / "archive"
    d.mkdir(parents=True, exist_ok=True)
    return d / f"{skel_hash}.jsonl"


def _append_archive_entry(platform: str, skel_hash: str, entry: dict) -> None:
    from spark.tasks.atomic_write import atomic_write_text

    path = _archive_log_path(platform, skel_hash)
    existing = ""
    if path.exists():
        try:
            existing = path.read_text(encoding="utf-8")
        except OSError as e:
            logger.warning(f"screen_session: unreadable archive log {path}: {e}")
    line = json.dumps(entry, ensure_ascii=True)
    content = f"{existing}{line}\n" if existing else f"{line}\n"
    atomic_write_text(path, content)


def _roll_live_window(platform: str, skel_hash: str, data: dict) -> None:
    rolled_attempts = []
    while len(data["attempts"]) > _MAX_LIVE_ATTEMPTS:
        rolled_attempts.append(data["attempts"].pop(0))
    rolled_lessons = []
    while len(data["lessons"]) > _MAX_LIVE_LESSONS:
        rolled_lessons.append(data["lessons"].pop(0))
    if rolled_attempts or rolled_lessons:
        _append_archive_entry(
            platform,
            skel_hash,
            {
                "reason": "live_window_roll",
                "rolled_at": _now(),
                "attempts": rolled_attempts,
                "lessons": rolled_lessons,
            },
        )


def _render_session_text(data: dict, attempts: list[dict], lessons: list[dict]) -> str:
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
    if attempts:
        parts.append(f"ATTEMPT HISTORY (live window: newest {len(attempts)}, older entries archived):")
        for i, a in enumerate(attempts, 1):
            acts = ",".join(a["bt_actions"]) if a["bt_actions"] else "?"
            author = a.get("author", "unknown")
            parts.append(f"  {i}. [{a['at']}] author={author} actions=[{acts}] -> {a['outcome']}"
                         + (f" | {a['detail']}" if a["detail"] else ""))
        parts.append("")
    if lessons:
        parts.append("LESSONS ON THIS SCREEN:")
        for l in lessons:
            author = l.get("author", "unknown")
            parts.append(f"  - {l['lesson']} (author={author})")
        parts.append("")
    parts.append("TO UPDATE THE SESSION: include a top-level \"_session\" object in your")
    parts.append("response JSON: {\"facts\": {...}, \"plan\": {...}, \"lesson\": \"...\"} —")
    parts.append("anything you measure or decide that the NEXT cycle must know.")
    return "\n".join(parts)


def _roll_render_bloat(platform: str, skel_hash: str, data: dict) -> tuple[list[dict], list[dict]]:
    attempts = list(data["attempts"][-_MAX_LIVE_ATTEMPTS:])
    lessons = list(data["lessons"][-_MAX_LIVE_LESSONS:])
    rendered = _render_session_text(data, attempts, lessons)
    if len(rendered) <= _SESSION_RENDER_CHAR_BUDGET:
        return attempts, lessons

    rolled_attempts = []
    rolled_lessons = []
    while len(rendered) > _SESSION_RENDER_CHAR_BUDGET and (attempts or lessons):
        next_attempt_at = attempts[0]["at"] if attempts else None
        next_lesson_at = lessons[0]["at"] if lessons else None
        if next_attempt_at is not None and (next_lesson_at is None or next_attempt_at <= next_lesson_at):
            entry = attempts.pop(0)
            rolled_attempts.append(entry)
            if data["attempts"] and data["attempts"][0] == entry:
                data["attempts"].pop(0)
        else:
            entry = lessons.pop(0)
            rolled_lessons.append(entry)
            if data["lessons"] and data["lessons"][0] == entry:
                data["lessons"].pop(0)
        rendered = _render_session_text(data, attempts, lessons)

    if rolled_attempts or rolled_lessons:
        _append_archive_entry(
            platform,
            skel_hash,
            {
                "reason": "render_budget_roll",
                "rolled_at": _now(),
                "attempts": rolled_attempts,
                "lessons": rolled_lessons,
                "budget_chars": _SESSION_RENDER_CHAR_BUDGET,
            },
        )
        _save(platform, skel_hash, data)
    return attempts, lessons


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
    _roll_live_window(platform, skel_hash, data)
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
    _roll_live_window(platform, skel_hash, data)
    _save(platform, skel_hash, data)


def archive(platform: str, skel_hash: str, reason: str = "advanced") -> None:
    """Screen advanced (or question changed): move raw session to archive."""
    p = _path(platform, skel_hash)
    if not p.exists():
        return
    try:
        payload = json.loads(p.read_text(encoding="utf-8"))
        _append_archive_entry(
            platform,
            skel_hash,
            {
                "reason": reason,
                "archived_at": _now(),
                "session": payload,
            },
        )
        p.unlink()
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
    attempts, lessons = _roll_render_bloat(platform, skel_hash, data)
    rendered = _render_session_text(data, attempts, lessons)
    if len(rendered) > _SESSION_RENDER_CHAR_BUDGET:
        logger.warning(
            "screen_session: render still exceeds budget for %s/%s (%s > %s)",
            platform,
            skel_hash[:12],
            len(rendered),
            _SESSION_RENDER_CHAR_BUDGET,
        )
    return rendered


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
