"""Append-only outcome log for learning."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

OUTCOMES_DIR = Path("/home/user/taey-ed/runtime/outcomes")
OUTCOME_LIMIT_DEFAULT = 200


def _outcome_path(platform: str) -> Path:
    return OUTCOMES_DIR / f"{platform}.jsonl"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _append_record(platform: str, record: dict) -> dict:
    path = _outcome_path(platform)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=True, separators=(",", ":")) + "\n")
    return record


def log_outcome(
    platform: str,
    screen_type: str,
    skeleton_hash: str,
    consultation_id: str,
    course_id: str,
    plan,
    success: bool,
    tier: int,
    wrong_answer_retry: bool,
    worker_fallback: bool,
    step2_validated: bool,
    error=None,
    fingerprint=None,
) -> dict:
    record = {
        "event_kind": "execution",
        "timestamp": _now(),
        "platform": platform,
        "screen_type": screen_type,
        "skeleton_hash": skeleton_hash,
        "consultation_id": consultation_id,
        "course_id": course_id,
        "plan": plan,
        "success": bool(success),
        "tier": int(tier),
        "wrong_answer_retry": bool(wrong_answer_retry),
        "worker_fallback": bool(worker_fallback),
        "step2_validated": bool(step2_validated),
        "error": error,
        "fingerprint": fingerprint,
    }
    return _append_record(platform, record)


def log_promotion_event(platform: str, skeleton_hash: str, entry: dict) -> dict:
    consults = list(entry.get("provenance_consults") or [])
    validated_courses = list(entry.get("validated_courses") or [])
    record = {
        "event_kind": "promotion",
        "timestamp": _now(),
        "platform": platform,
        "screen_type": str(entry.get("screen_type") or ""),
        "skeleton_hash": skeleton_hash,
        "consultation_id": consults[-1] if consults else "",
        "course_id": validated_courses[-1] if validated_courses else "",
        "plan": entry,
        "success": True,
        "tier": 0,
        "wrong_answer_retry": False,
        "worker_fallback": False,
        "step2_validated": True,
        "error": None,
        "fingerprint": "promotion_event",
    }
    return _append_record(platform, record)


def get_platform_outcomes(platform: str, limit: int = OUTCOME_LIMIT_DEFAULT) -> list[dict]:
    path = _outcome_path(platform)
    if not path.exists():
        return []
    records: list[dict] = []
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            records.append(json.loads(line))
    return records[-limit:] if limit > 0 else records


def get_outcomes_for(skeleton_hash: str, platform: str, limit: int = OUTCOME_LIMIT_DEFAULT) -> list[dict]:
    outcomes = get_platform_outcomes(platform, limit=limit)
    return [
        outcome
        for outcome in outcomes
        if outcome.get("event_kind", "execution") == "execution"
        and outcome.get("skeleton_hash") == skeleton_hash
        and outcome.get("platform") == platform
    ]
