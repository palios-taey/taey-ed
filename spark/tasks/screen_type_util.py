"""
Screen type hierarchy utilities.

Master categories (IMS Caliper validated):
    VIDEO, ARTICLE, EXERCISE, NAVIGATION, TRANSITION, UNKNOWN

Variants are platform-specific and open-ended:
    VIDEO:playing, VIDEO:complete, EXERCISE:radio, EXERCISE:checkbox, etc.

Backward compat: ARTICLE_READING -> master=ARTICLE, variant=reading
"""

# Master categories that use deterministic stored BTs (no Gemini needed)
DETERMINISTIC_CATEGORIES = {"VIDEO", "ARTICLE"}

# All recognized master categories
MASTER_CATEGORIES = {"VIDEO", "ARTICLE", "EXERCISE", "NAVIGATION", "TRANSITION", "UNKNOWN"}


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


def is_deterministic(screen_type: str) -> bool:
    """Return True if this screen type should use stored BTs without Gemini."""
    return get_master_category(screen_type) in DETERMINISTIC_CATEGORIES
