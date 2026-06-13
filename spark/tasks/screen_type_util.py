"""
Screen type hierarchy utilities.

Master categories (IMS Caliper validated):
    VIDEO, ARTICLE, EXERCISE, NAVIGATION, TRANSITION, UNKNOWN

Variants are platform-specific and open-ended:
    VIDEO:playing, VIDEO:complete, EXERCISE:radio, EXERCISE:checkbox, etc.

Backward compat: ARTICLE_READING -> master=ARTICLE, variant=reading
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

# All recognized master categories
MASTER_CATEGORIES = {"VIDEO", "ARTICLE", "EXERCISE", "NAVIGATION", "TRANSITION", "UNKNOWN"}
_DEFAULT_PLATFORM = "khan_academy"


def _platforms_dir() -> Path:
    candidates = [
        Path(__file__).parent.parent / "platforms",
        Path("spark/platforms"),
    ]
    for path in candidates:
        if path.exists():
            return path
    return candidates[0]


@lru_cache(maxsize=64)
def _load_screen_type_metadata(platform: str, screen_type: str) -> dict:
    path = _platforms_dir() / platform / "screen_types" / f"{screen_type}.yaml"
    if not path.exists():
        return {}
    try:
        text = path.read_text(encoding="utf-8")
        for line in text.splitlines():
            if line.startswith("deterministic:"):
                value = line.split(":", 1)[1].strip().split("#", 1)[0].strip().lower()
                return {"deterministic": value == "true"}
    except Exception:
        return {}
    return {}


def get_master_category(screen_type: str) -> str:
    """Extract master category from screen_type string.

    Supports new (colon) and legacy (underscore) formats, plus
    platform-prefixed variants:
        "VIDEO:playing"              -> "VIDEO"
        "ARTICLE_READING"            -> "ARTICLE"
        "EXERCISE"                   -> "EXERCISE"
        "KA_EXERCISE_DRAG_RANKING"   -> "EXERCISE"     (platform prefix)
        "KA_COURSE_OVERVIEW"         -> "NAVIGATION"   (semantic platform variant)
    """
    if not screen_type:
        return "UNKNOWN"

    # New format: colon separator
    if ":" in screen_type:
        master = screen_type.split(":")[0].upper()
        if master in MASTER_CATEGORIES:
            return master
        return "UNKNOWN"

    # Legacy format: check if prefix matches a master category
    upper = screen_type.upper()
    for cat in sorted(MASTER_CATEGORIES, key=len, reverse=True):
        if upper == cat or upper.startswith(cat + "_"):
            return cat

    # Strip platform prefix (e.g. "KA_") and try again. Classifier may return
    # platform-specific variants like "KA_EXERCISE_DRAG_RANKING" where the
    # master category sits AFTER the 2-letter platform prefix.
    if "_" in upper:
        without_prefix = upper.split("_", 1)[1]
        for cat in sorted(MASTER_CATEGORIES, key=len, reverse=True):
            if without_prefix == cat or without_prefix.startswith(cat + "_"):
                return cat

    return "UNKNOWN"


def is_deterministic(screen_type: str, platform: str = _DEFAULT_PLATFORM) -> bool:
    """Return True iff the matched YAML marks this screen type deterministic."""
    metadata = _load_screen_type_metadata(platform, screen_type)
    if metadata:
        return bool(metadata.get("deterministic", False))
    return False
