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

    Supports both new (colon) and legacy (underscore) formats:
        "VIDEO:playing" -> "VIDEO"
        "ARTICLE_READING" -> "ARTICLE"
        "EXERCISE" -> "EXERCISE"
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

    return "UNKNOWN"


def is_deterministic(screen_type: str) -> bool:
    """Return True if this screen type should use stored BTs without Gemini."""
    return get_master_category(screen_type) in DETERMINISTIC_CATEGORIES
