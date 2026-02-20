# STATUS: FROZEN - Bug-fixed from v7. Verified 2026-02-20. Do not modify.
"""
Layer 4: The Screen Router

Replaces the consultation pipeline with embedding-based screen recognition.
Three-tier routing:

  KNOWN     (distance < 0.05):  Execute stored behavior tree directly
  ISOMORPHIC (distance < 0.191): Same structure, different content.
             Extract dynamic text, ask LLM for answer, execute stored BT.
  UNCHARTED  (distance >= 0.191): Never seen this structure.
             Send full context to LLM for new behavior tree.

The threshold 0.191 = 1 - phi^-1 (golden ratio complement).
"""

import json
import logging
import time
from typing import Optional

from .skeleton import extract_skeleton, skeleton_hash, extract_dynamic_text
from .screen_memory import embed_text, query_nearest, get_client

logger = logging.getLogger("screen_router")

# Routing thresholds
KNOWN_THRESHOLD = 0.05       # Below this = exact structural match
ISOMORPHIC_THRESHOLD = 0.191  # Below this = same type, different content


class RouteResult:
    """Result of routing a screen through the spinal cord."""

    def __init__(
        self,
        category: str,  # "KNOWN", "ISOMORPHIC", "UNCHARTED"
        behavior_tree: Optional[dict] = None,
        dynamic_text: Optional[list[str]] = None,
        skeleton: str = "",
        skeleton_hash_val: str = "",
        embedding: Optional[list[float]] = None,
        distance: float = 1.0,
        match_data: Optional[dict] = None,
        screen_type: str = "",
    ):
        self.category = category
        self.behavior_tree = behavior_tree
        self.dynamic_text = dynamic_text
        self.skeleton = skeleton
        self.skeleton_hash = skeleton_hash_val
        self.embedding = embedding
        self.distance = distance
        self.match_data = match_data
        self.screen_type = screen_type

    def to_dict(self) -> dict:
        return {
            "category": self.category,
            "screen_type": self.screen_type,
            "behavior_tree": self.behavior_tree,
            "dynamic_text": self.dynamic_text,
            "skeleton_hash": self.skeleton_hash,
            "distance": self.distance,
            "success_count": self.match_data.get("success_count", 0) if self.match_data else 0,
        }


def route_screen(
    tree: dict,
    platform: str,
    viewport_height: int = 900,
) -> RouteResult:
    """
    Route a screen tree through the spinal cord.

    Args:
        tree: Accessibility tree dict from Mac capture_tree
        platform: Platform name (khan_academy, coursera, etc.)
        viewport_height: For skeleton vertical position calculation

    Returns:
        RouteResult with category, behavior_tree, and supporting data
    """
    t0 = time.time()

    # Layer 1: Extract skeleton
    skel = extract_skeleton(tree, viewport_height=viewport_height)
    shash = skeleton_hash(skel)

    # Layer 2: Embed
    vec = embed_text(skel)

    # Layer 3: Query Weaviate
    client = get_client()
    try:
        matches = query_nearest(vec, platform=platform, limit=1, client=client)
    finally:
        client.close()

    t1 = time.time()
    logger.info(f"Route lookup: {t1-t0:.3f}s, skeleton_hash={shash}")

    # No matches at all → UNCHARTED
    if not matches:
        texts = extract_dynamic_text(tree)
        return RouteResult(
            category="UNCHARTED",
            dynamic_text=texts,
            skeleton=skel,
            skeleton_hash_val=shash,
            embedding=vec,
        )

    match = matches[0]
    distance = match["distance"]

    if distance < KNOWN_THRESHOLD:
        # KNOWN: Execute stored BT directly
        bt = json.loads(match["behavior_tree"]) if isinstance(match["behavior_tree"], str) else match["behavior_tree"]
        stype = match.get("screen_type", "")
        logger.info(f"KNOWN screen: hash={shash} type={stype} distance={distance:.4f} successes={match['success_count']}")
        return RouteResult(
            category="KNOWN",
            behavior_tree=bt,
            skeleton=skel,
            skeleton_hash_val=shash,
            embedding=vec,
            distance=distance,
            match_data=match,
            screen_type=stype,
        )

    elif distance < ISOMORPHIC_THRESHOLD:
        # ISOMORPHIC: Same structure, need LLM for content-specific answer
        bt = json.loads(match["behavior_tree"]) if isinstance(match["behavior_tree"], str) else match["behavior_tree"]
        texts = extract_dynamic_text(tree)
        stype = match.get("screen_type", "")
        logger.info(f"ISOMORPHIC screen: hash={shash} type={stype} distance={distance:.4f} texts={len(texts)}")
        return RouteResult(
            category="ISOMORPHIC",
            behavior_tree=bt,
            dynamic_text=texts,
            skeleton=skel,
            skeleton_hash_val=shash,
            embedding=vec,
            distance=distance,
            match_data=match,
            screen_type=stype,
        )

    else:
        # UNCHARTED: New screen structure
        texts = extract_dynamic_text(tree)
        logger.info(f"UNCHARTED screen: hash={shash} distance={distance:.4f}")
        return RouteResult(
            category="UNCHARTED",
            dynamic_text=texts,
            skeleton=skel,
            skeleton_hash_val=shash,
            embedding=vec,
            distance=distance,
            match_data=match,
        )
