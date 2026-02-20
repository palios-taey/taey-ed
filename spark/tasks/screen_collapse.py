# STATUS: FROZEN - Proven in v7. Verified 2026-02-19. Do not modify.
"""
Layer 5: The Collapse

After every successful action, compare tree hash before/after.
If the screen changed (hash_before != hash_after), the action succeeded.
Store the embedding + behavior tree in Weaviate for future recognition.

Called async after action execution completes on the Mac side.
"""

import hashlib
import json
import logging
from typing import Optional

from .screen_memory import store_screen, get_client
from .skeleton import extract_skeleton, skeleton_hash

logger = logging.getLogger("screen_collapse")


def compute_tree_hash(tree: dict) -> str:
    """
    SHA256 hash of tree for change detection.
    Matches the Mac-side compute_tree_hash implementation.
    Returns first 16 chars of hex digest.
    """
    def extract_relevant(node: dict) -> list:
        result = []
        role = node.get("role", "")
        name = node.get("name", "")
        if role or name:
            result.append(f"{role}:{name}")
        for child in node.get("children", []):
            result.extend(extract_relevant(child))
        return result

    relevant = sorted(extract_relevant(tree))
    content = "|".join(relevant)
    return hashlib.sha256(content.encode()).hexdigest()[:16]


def collapse(
    tree_before: dict,
    tree_after: dict,
    embedding: list[float],
    behavior_tree: dict,
    platform: str,
    skeleton_text: str = "",
    skeleton_hash_val: str = "",
    screen_type: str = "",
) -> dict:
    """
    Post-action collapse: if screen changed, store the successful BT.

    Args:
        tree_before: Accessibility tree BEFORE action
        tree_after: Accessibility tree AFTER action
        embedding: The skeleton embedding vector (from route_screen)
        behavior_tree: The BT that was executed
        platform: Platform name
        skeleton_text: Raw skeleton string (for debugging)
        skeleton_hash_val: Pre-computed skeleton hash
        screen_type: Screen type tag (QUIZ_MULTIPLE_CHOICE, etc.)

    Returns:
        {"collapsed": bool, "hash_before": str, "hash_after": str}
    """
    hash_before = compute_tree_hash(tree_before)
    hash_after = compute_tree_hash(tree_after)

    if hash_before == hash_after:
        logger.info(f"No state change: hash={hash_before} — action may have failed")
        return {
            "collapsed": False,
            "hash_before": hash_before,
            "hash_after": hash_after,
            "reason": "no_state_change",
        }

    # State changed = success. Store in Weaviate.
    if not skeleton_hash_val:
        skel = extract_skeleton(tree_before)
        skeleton_hash_val = skeleton_hash(skel)
        skeleton_text = skel

    client = get_client()
    try:
        store_screen(
            vector=embedding,
            skeleton_hash=skeleton_hash_val,
            platform=platform,
            behavior_tree=behavior_tree,
            skeleton_text=skeleton_text,
            screen_type=screen_type,
            client=client,
        )
    finally:
        client.close()

    logger.info(
        f"Collapsed: hash {hash_before} → {hash_after}, "
        f"stored skeleton={skeleton_hash_val}"
    )

    return {
        "collapsed": True,
        "hash_before": hash_before,
        "hash_after": hash_after,
        "skeleton_hash": skeleton_hash_val,
    }
