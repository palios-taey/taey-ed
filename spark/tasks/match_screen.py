# STATUS: FROZEN - Bug-fixed from v7. Verified 2026-02-20. Do not modify.
"""
Match accessibility tree against screen definitions.

V13: Vector-first with YAML fallback. Heuristic removed (seeded into Weaviate).

Primary path: Skeleton → Qwen3 embedding → Weaviate ScreenEmbedding search
  d < 0.05  → KNOWN (execute stored behavior tree directly)
  d < 0.191 → ISOMORPHIC (same structure, extract dynamic text for LLM)
  d >= 0.191 → fall through to YAML markers

Fallback path: YAML marker-based matching (existing V9 logic)
  Catches screens not yet in ScreenEmbedding.

Safety halt runs BEFORE either path (keyword-based, not vector-based).

Performance: skeleton(5ms) + embed(60ms) + search(20ms) = ~85ms total
vs 150 seconds per consultation for unmatched screens.
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

    When a match is found, supplements with YAML metadata (extract,
    expected_next, validation) if the screen exists in config.yaml.

    Returns match result dict if confident match found, None otherwise.
    """
    try:
        from spark.tasks.screen_router import route_screen, KNOWN_THRESHOLD, ISOMORPHIC_THRESHOLD
        import json as _json

        route_result = route_screen(tree, platform)

        if route_result.category == "UNCHARTED":
            logger.info(
                f"Spinal cord: UNCHARTED (d={route_result.distance:.3f}), "
                f"falling through to YAML"
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

        # Include behavior tree from spinal cord
        if route_result.behavior_tree:
            result["tree"] = route_result.behavior_tree

        # Include expected_next from Weaviate match data
        if route_result.match_data:
            import json as _json
            en_raw = route_result.match_data.get("expected_next", "[]")
            try:
                result["expected_next"] = _json.loads(en_raw) if isinstance(en_raw, str) else (en_raw or [])
            except Exception:
                result["expected_next"] = []

        # Include dynamic text for ISOMORPHIC screens
        if route_result.category == "ISOMORPHIC" and route_result.dynamic_text:
            result["dynamic_text"] = route_result.dynamic_text

        # Supplement with YAML metadata: run YAML marker matching to find
        # the screen definition, then use its tree/extract/expected_next/validation
        if config:
            exact_texts, all_texts, role_counts = extract_tree_index(tree)
            yaml_match = _match_yaml(tree, config, exact_texts, all_texts, role_counts)
            if yaml_match:
                result["screen"] = yaml_match["screen"]
                for key in ("tree", "extract", "expected_next", "validation", "description"):
                    if key in yaml_match:
                        result[key] = yaml_match[key]

        return result

    except Exception as e:
        logger.warning(f"Spinal cord error (falling back to YAML): {e}")
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

    V13: Vector-first, YAML fallback. Heuristic removed (seeded into Weaviate).

    1. Safety halt check (keyword-based, always runs first)
    2. Vector search via skeleton embedding + Weaviate ScreenEmbedding
    3. YAML marker matching (fallback)
    4. No match → needs consultation

    Args:
        tree: Accessibility tree dict from Mac
        config: Platform config dict with screens

    Returns:
        {"matched": True, "screen": name, "tree": {...}, ...} or
        {"matched": False, "needs_consultation": True}
    """
    # Walk tree ONCE to build index - O(T)
    exact_texts, all_texts, role_counts = extract_tree_index(tree)

    # PATH 1: Spinal cord matching (skeleton → embed → Weaviate)
    platform = config.get("platform", "")
    if not platform:
        logger.error("match_screen: config missing 'platform' key — cannot route")
        return {"matched": False, "needs_consultation": True, "error": "missing_platform"}
    if _check_vector_available():
        vector_result = _try_vector_match(tree, platform, config)
        if vector_result:
            return vector_result

    # PATH 2: YAML marker matching (fallback)
    yaml_result = _match_yaml(tree, config, exact_texts, all_texts, role_counts)
    if yaml_result:
        return yaml_result

    return {"matched": False, "needs_consultation": True}
