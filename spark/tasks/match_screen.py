"""
Match accessibility tree against screen definitions.

V17: Set-difference discriminative matching. No Weaviate.

Extract (role, text) signatures → subtract common chrome → match on
discriminative markers. JSON file storage per platform.
"""

import logging
from collections import Counter

logger = logging.getLogger(__name__)


def extract_tree_index(tree: dict) -> tuple[set, list, Counter]:
    """
    Walk tree ONCE, extract all text values and role counts.

    Returns:
        exact_texts: set of all exact text values (for O(1) lookup)
        all_texts: list of all text values (for substring search)
        role_counts: Counter of role -> count
    """
    exact_texts = set()
    all_texts = []
    role_counts = Counter()

    stack = [tree]
    while stack:
        node = stack.pop()

        # Collect text from all text fields
        for field in ("name", "value", "title", "description"):
            val = node.get(field)
            if val is not None:
                s = str(val)
                exact_texts.add(s)
                all_texts.append(s)

        # Count roles
        role = node.get("role")
        if role:
            role_counts[role] += 1

        # Push children (iterate in reverse to maintain order, though order doesn't matter here)
        children = node.get("children")
        if children:
            stack.extend(children)

    return exact_texts, all_texts, role_counts


def check_marker_indexed(marker: dict, exact_texts: set, all_texts: list, role_counts: Counter) -> bool:
    """
    Check if a single marker matches using pre-indexed tree data.

    Marker types:
    - {"text": "exact string"} - O(1) set lookup
    - {"text": "partial", "match": "contains"} - O(T) substring scan
    - {"text": "X", "present": false} - NEGATIVE marker: true only if X is NOT found
    - {"role": "AXButton", "count_min": 3} - O(1) counter lookup
    """
    # Guard: if marker is a plain string (legacy format), convert to dict
    if isinstance(marker, str):
        marker = {"text": marker}

    # Determine if this is a negative marker (present: false)
    want_present = marker.get("present", True)

    if "text" in marker:
        text = marker["text"]
        mode = marker.get("match", "exact")
        if mode == "contains":
            found = any(text in s for s in all_texts)
        else:
            found = text in exact_texts
        return found if want_present else not found

    elif "role" in marker:
        role = marker["role"]
        count = role_counts.get(role, 0)
        if "count_min" in marker:
            found = count >= marker["count_min"]
        else:
            found = count > 0
        return found if want_present else not found

    # Unknown marker type - skip (don't fail)
    return True


def match_screen(tree: dict, config: dict) -> dict:
    """
    Match tree against known screen signatures using set-difference.

    Args:
        tree: Accessibility tree dict from Mac
        config: Platform config dict (must have 'platform' key)

    Returns:
        {"matched": True, "screen": name, "screen_type": ..., "tree": {...}, ...} or
        {"matched": False, "needs_consultation": True}
    """
    platform = config.get("platform", "")
    if not platform:
        logger.error("match_screen: config missing 'platform' key — cannot route")
        return {"matched": False, "needs_consultation": True, "error": "missing_platform"}

    from spark.tasks.screen_signatures import match_signature
    result = match_signature(platform, tree)

    if result.get("matched"):
        logger.info(f"Signature match: {result['screen_type']} "
                     f"(score={result['match_score']:.2f}, hash={result['sig_hash']})")
        return {
            "matched": True,
            "screen": result["screen_type"],
            "screen_type": result["screen_type"],
            "match_source": "signature",
            "sig_hash": result["sig_hash"],
            "match_score": result["match_score"],
            "validated": result.get("validated", False),
            "tree": result.get("tree"),
        }

    logger.info(f"No signature match for {platform}")
    return {"matched": False, "needs_consultation": True}
