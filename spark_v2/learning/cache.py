"""Cache class taxonomy and cached BT storage."""

from __future__ import annotations

import json
from pathlib import Path

from spark_v2.tasks.knowledge_loader import _empty_shell
from spark_v2.utils.atomic_write import atomic_write_json

PLATFORMS_DIR = Path(__file__).resolve().parents[1] / "platforms"

VERIFIED_COUNT_MIN = 5
LOWER_CI_THRESHOLD = 0.95
CROSS_UNIT_HOLDOUT_MIN = 2
CLEANLINESS_REQUIRED = 1.0
INVALIDATION_AT = 2

DETERMINISTIC_SCREEN_TYPES = frozenset({"NAVIGATION", "VIDEO", "ARTICLE", "TRANSITION"})
PROCEDURAL_SCREEN_TYPES = frozenset({"EXERCISE"})


def _master_screen_type(screen_type: str) -> str:
    return str(screen_type or "").split("_", 1)[0].split(":", 1)[0].upper()


def _determine_cache_class(screen_type: str) -> str:
    master = _master_screen_type(screen_type)
    if master == "NAVIGATION":
        return "NO_CACHE"
    if master in DETERMINISTIC_SCREEN_TYPES:
        return "DETERMINISTIC_BT"
    if master in PROCEDURAL_SCREEN_TYPES:
        return "PROCEDURAL_TEMPLATE"
    return "NO_CACHE"


def _knowledge_path(platform: str) -> Path:
    return PLATFORMS_DIR / platform / "knowledge.json"


def _load_writable_knowledge(platform: str) -> dict:
    path = _knowledge_path(platform)
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    return _empty_shell(platform)


def load_cached_bt(platform: str, skeleton_hash: str) -> dict | None:
    path = _knowledge_path(platform)
    if not path.exists():
        return None
    knowledge = json.loads(path.read_text(encoding="utf-8"))
    cached_bts = knowledge.get("cached_bts", {})
    if not isinstance(cached_bts, dict):
        return None
    entry = cached_bts.get(skeleton_hash)
    return entry if isinstance(entry, dict) else None


def store_cached_bt(platform: str, skeleton_hash: str, entry: dict) -> dict:
    knowledge = _load_writable_knowledge(platform)
    platform_dir = _knowledge_path(platform).parent
    platform_dir.mkdir(parents=True, exist_ok=True)
    cached_bts = knowledge.setdefault("cached_bts", {})
    cached_bts[skeleton_hash] = entry
    atomic_write_json(_knowledge_path(platform), knowledge)
    return entry


def invalidate_cached_bt(platform: str, skeleton_hash: str) -> bool:
    path = _knowledge_path(platform)
    if not path.exists():
        return False
    knowledge = json.loads(path.read_text(encoding="utf-8"))
    cached_bts = knowledge.get("cached_bts", {})
    if not isinstance(cached_bts, dict) or skeleton_hash not in cached_bts:
        return False
    del cached_bts[skeleton_hash]
    atomic_write_json(path, knowledge)
    return True
