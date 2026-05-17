"""Knowledge loading helpers for spark_v2."""

from __future__ import annotations

import json
from pathlib import Path

from spark_v2.utils.atomic_write import atomic_write_json

PLATFORMS_DIR = Path(__file__).resolve().parents[1] / "platforms"


def _platform_dir(platform: str) -> Path:
    return PLATFORMS_DIR / platform


def _display_name(platform: str) -> str:
    return " ".join(part.capitalize() for part in platform.split("_")) or platform


def _empty_shell(platform: str) -> dict:
    return {
        "schema_version": "v2",
        "platform": {
            "name": platform,
            "display_name": _display_name(platform),
            "url_pattern": platform,
            "platform_type": "other",
            "framework_hint": None,
            "discovered_at": None,
            "discovery_source": None,
        },
        "global": {
            "completion_indicators": [],
            "advancement_link_patterns": [],
            "video_completion_signal": None,
            "timing_characteristics": None,
        },
        "screen_patterns": {},
        "never_clicks_platform": [],
        "widget_classes": {},
        "cached_bts": {},
        "_meta": {
            "created_at": None,
            "last_updated_at": None,
            "discovery_event_id": None,
            "schema_version": "v2",
            "validating_consults_total": 0,
            "discovery_consults_total": 0,
            "recovery_consults_total": 0,
            "user_assist_events_total": 0,
        },
    }


def is_first_touch(platform_data: dict) -> bool:
    if not platform_data:
        return True
    if any(platform_data.get("global", {}).get(key) for key in ("completion_indicators", "advancement_link_patterns")):
        return False
    if platform_data.get("global", {}).get("video_completion_signal") is not None:
        return False
    if platform_data.get("global", {}).get("timing_characteristics") is not None:
        return False
    if platform_data.get("screen_patterns"):
        return False
    if platform_data.get("never_clicks_platform"):
        return False
    if platform_data.get("widget_classes"):
        return False
    if platform_data.get("cached_bts"):
        return False
    return True


def load_knowledge(platform: str) -> dict:
    path = _platform_dir(platform) / "knowledge.json"
    if not path.exists():
        return _empty_shell(platform)
    data = json.loads(path.read_text())
    if is_first_touch(data):
        return data
    return data


def load_provisional(platform: str) -> dict | None:
    path = _platform_dir(platform) / "provisional_knowledge.json"
    if not path.exists():
        return None
    data = json.loads(path.read_text())
    return data if data else None


def merge_provisional_to_global(platform: str, provisional_data: dict) -> dict:
    platform_dir = _platform_dir(platform)
    platform_dir.mkdir(parents=True, exist_ok=True)
    path = platform_dir / "provisional_knowledge.json"
    atomic_write_json(path, provisional_data)
    return provisional_data
