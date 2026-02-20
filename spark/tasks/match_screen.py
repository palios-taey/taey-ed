"""
Match accessibility tree against screen definitions.

V16: Vector-only. No YAML.

Skeleton → Qwen3 embedding → Weaviate ScreenEmbedding search
  d < 0.05  → KNOWN (execute stored behavior tree directly)
  d < 0.191 → ISOMORPHIC (same structure, execute stored BT)
  d >= 0.191 → UNCHARTED → navigation auto-detect or consultation

Vector store grows organically through consultations. First encounter
of any screen type → consultation → BT stored in Weaviate → matches
automatically on subsequent visits. No YAML markers anywhere.
"""

import logging
from collections import Counter

logger = logging.getLogger(__name__)

# Lazy imports for vector matching (avoid import errors if deps missing)
_vector_available = None


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



def _check_vector_available() -> bool:
    """Check if spinal cord (ScreenEmbedding) is available.

    Always checks fresh — no caching. The collection starts empty and grows
    organically as consultations teach new screens. Caching would prevent
    vectors from activating after the first screen is learned.
    """
    try:
        from spark.tasks.screen_memory import get_stats
        stats = get_stats()
        available = stats.get("exists", False) and stats.get("count", 0) > 0
        return available
    except Exception as e:
        logger.debug(f"Spinal cord unavailable: {e}")
        return False


def _try_vector_match(tree: dict, platform: str, config: dict = None) -> dict | None:
    """
    Try to match screen via spinal cord (skeleton → embed → Weaviate).

    Uses the Phase 8 spinal cord architecture:
      skeleton.py → extract structure
      screen_memory.py → embed + query ScreenEmbedding

    Returns match result dict if confident match found, None otherwise.
    Everything in the result comes from Weaviate (BT, extract, expected_next).
    No YAML supplementation.
    """
    try:
        from spark.tasks.screen_router import route_screen, KNOWN_THRESHOLD, ISOMORPHIC_THRESHOLD
        import json as _json

        route_result = route_screen(tree, platform)

        if route_result.category == "UNCHARTED":
            logger.info(
                f"Spinal cord: UNCHARTED (d={route_result.distance:.3f}), "
                f"no match"
            )
            return None

        # KNOWN or ISOMORPHIC — we have a match
        logger.info(
            f"Spinal cord: {route_result.category} "
            f"(d={route_result.distance:.4f}, hash={route_result.skeleton_hash})"
        )

        result = {
            "matched": True,
            "screen": route_result.screen_type or f"spinal_{route_result.skeleton_hash[:8]}",
            "screen_type": route_result.screen_type,
            "match_type": route_result.category,
            "match_distance": route_result.distance,
            "match_source": "spinal_cord",
            "skeleton_hash": route_result.skeleton_hash,
            "embedding": route_result.embedding,
            "validated": route_result.match_data.get("validated", False) if route_result.match_data else False,
        }

        # Include behavior tree from Weaviate
        if route_result.behavior_tree:
            result["tree"] = route_result.behavior_tree

        # Include expected_next from Weaviate match data
        if route_result.match_data:
            en_raw = route_result.match_data.get("expected_next", "[]")
            try:
                result["expected_next"] = _json.loads(en_raw) if isinstance(en_raw, str) else (en_raw or [])
            except Exception:
                result["expected_next"] = []

            # Include extract config from Weaviate if stored
            extract_raw = route_result.match_data.get("extract")
            if extract_raw:
                try:
                    result["extract"] = _json.loads(extract_raw) if isinstance(extract_raw, str) else extract_raw
                except Exception:
                    pass

        # Include dynamic text for ISOMORPHIC screens
        if route_result.category == "ISOMORPHIC" and route_result.dynamic_text:
            result["dynamic_text"] = route_result.dynamic_text

        return result

    except Exception as e:
        logger.warning(f"Spinal cord error: {e}")
        return None


def _match_yaml(tree: dict, config: dict, exact_texts: set, all_texts: list, role_counts: Counter) -> dict | None:
    """
    YAML marker-based matching (V9 logic, kept as fallback).

    Returns match result dict or None if no match.
    """
    best_match = None
    best_score = -1.0

    for screen_name, screen_def in config.get("screens", {}).items():
        markers = screen_def.get("markers", [])

        all_found = all(
            check_marker_indexed(m, exact_texts, all_texts, role_counts)
            for m in markers
        )

        if all_found:
            score = 0.0
            for m in markers:
                if not m.get("present", True):
                    score += 1.5
                elif m.get("match") == "contains":
                    score += 1.0
                else:
                    score += 1.1

            if score > best_score:
                best_score = score
                best_match = (screen_name, screen_def)

    if best_match is None:
        return None

    screen_name, screen_def = best_match
    result = {
        "matched": True,
        "screen": screen_name,
        "match_source": "yaml",
    }

    if "tree" in screen_def:
        result["tree"] = screen_def["tree"]
    if "extract" in screen_def:
        result["extract"] = screen_def["extract"]
    if "description" in screen_def:
        result["description"] = screen_def["description"]
    if "expected_next" in screen_def:
        result["expected_next"] = screen_def["expected_next"]
    if "validation" in screen_def:
        result["validation"] = screen_def["validation"]

    return result



def match_screen(tree: dict, config: dict) -> dict:
    """
    Match tree against screen definitions.

    V16: Vector-only. No YAML.

    1. Vector search via skeleton embedding + Weaviate ScreenEmbedding
    2. No match → needs consultation or navigation auto-detect (handled by next_action.py)

    Args:
        tree: Accessibility tree dict from Mac
        config: Platform config dict with screens

    Returns:
        {"matched": True, "screen": name, "tree": {...}, ...} or
        {"matched": False, "needs_consultation": True}
    """
    platform = config.get("platform", "")
    if not platform:
        logger.error("match_screen: config missing 'platform' key — cannot route")
        return {"matched": False, "needs_consultation": True, "error": "missing_platform"}

    if _check_vector_available():
        vector_result = _try_vector_match(tree, platform, config)
        if vector_result:
            return vector_result

    return {"matched": False, "needs_consultation": True}
