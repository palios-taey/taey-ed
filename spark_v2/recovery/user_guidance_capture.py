"""Capture validated user guidance events for Tier 3 learning."""

from __future__ import annotations

import json
import time
from pathlib import Path

from spark_v2.learning.outcome_log import log_event
from spark_v2.tasks.knowledge_loader import increment_meta_counter
from spark_v2.utils.atomic_write import atomic_write_json

GUIDANCE_DIR = Path("/tmp/taey-ed-user-guidance")


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def capture_user_guidance(
    *,
    platform: str,
    consultation_id: str,
    course_id: str,
    screen_type: str,
    tree_hash: str,
    guidance_text: str,
) -> dict:
    timestamp = _now()
    event = {
        "captured_at": timestamp,
        "platform": platform,
        "consultation_id": consultation_id,
        "course_id": course_id,
        "screen_type": screen_type,
        "tree_hash": tree_hash,
        "guidance_text": guidance_text,
    }
    guidance_path = GUIDANCE_DIR / f"{consultation_id}.json"
    guidance_path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_json(guidance_path, event)
    increment_meta_counter(platform, "user_assist_events_total")
    log_event(
        platform,
        event_kind="tier3_captured",
        screen_type=screen_type,
        skeleton_hash=tree_hash,
        consultation_id=consultation_id,
        course_id=course_id,
        payload={"guidance_text": guidance_text},
    )
    return json.loads(guidance_path.read_text(encoding="utf-8"))
