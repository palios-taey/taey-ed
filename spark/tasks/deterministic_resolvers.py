"""
Deterministic, LLM-free target resolvers for known screen variants.

When a variant has a stable structural rule for picking the next clickable
target from the AX tree, we walk the tree server-side and emit a 1-action BT
with the exact target name. No worker call, no LLM, no guessing.

Per Jesse 2026-05-19: "It needs to click the right one, no guessing. You can
use text before/after to match. No guessing."

Registry: VARIANT_RESOLVERS maps variant string → resolver(tree) → str | None.
A resolver returns the EXACT AX `name` (or `description`) string of the link
to click, which the caller plugs into a `find_and_click target=... role=AXLink
match_mode=exact` BT. The name is guaranteed unique-on-screen because we pick
visible-bbox candidates whose names carry the state suffix (": unfamiliar",
"Up next for you!", etc.) that the invisible screen-reader outline duplicates
do not carry.
"""
from __future__ import annotations

import logging
import re
from typing import Callable, Optional

logger = logging.getLogger(__name__)

UNIT_BANNER_RE = re.compile(r"^Unit \d+ Up next for you!$", re.IGNORECASE)


def _walk_axlinks_dom_order(tree: dict):
    """Yield (name, bbox, element_id) for every AXLink in DOM order.
    DOM order = depth-first, children visited in their stored order.
    """
    stack = [tree]
    # Use a deque-like traversal that respects child order: append children
    # in reverse so pop() yields them in original order.
    while stack:
        n = stack.pop()
        if not isinstance(n, dict):
            continue
        if n.get("role") == "AXLink":
            name = (n.get("name") or "").strip()
            bbox = n.get("visible_bbox") or n.get("position") or [0, 0, 0, 0]
            eid = n.get("element_id") or ""
            yield (name, bbox, eid)
        children = n.get("children") or []
        for c in reversed(children):
            stack.append(c)


def _is_visible(bbox) -> bool:
    """A bbox is visible if both width and height are > 0."""
    if not bbox or len(bbox) < 4:
        return False
    return bbox[2] > 0 and bbox[3] > 0


def resolve_khan_course_overview_target(tree: dict) -> Optional[str]:
    """Resolve the next target on a Khan Academy course overview page.

    Rule (deterministic, no LLM):

      Pass 1 — "Up next for you!" callout (when the platform highlights it):
        first visible AXLink whose name ends with "Up next for you!"
        AND does NOT match the generic "Unit N Up next for you!" banner.

      Pass 2 — mid-progress (no callout):
        first visible AXLink (DOM order) whose name ends with ": unfamiliar".

    Returns the AXLink's exact `name` string, or None if neither pass matches.

    Why name uniqueness holds:
      The invisible screen-reader outline links carry SHORTER names without
      the state suffix (e.g. "Apply: Kinetic energy") while the visible
      mastery-grid icons carry the full suffixed name (e.g. "Apply: Kinetic
      energy: unfamiliar"). Exact-match on the suffixed name targets the
      visible icon every time.
    """
    pass1_match = None
    pass2_match = None

    for name, bbox, eid in _walk_axlinks_dom_order(tree):
        if not name or not _is_visible(bbox):
            continue

        # Pass 1: "Up next for you!" callout target, excluding the generic
        # unit-level banner (e.g. "Unit 1 Up next for you!").
        if name.endswith("Up next for you!") and not UNIT_BANNER_RE.match(name):
            if pass1_match is None:
                pass1_match = name
                # Don't break — log all matches at info level for debugging.

        # Pass 2: first ": unfamiliar" link in DOM order.
        if pass2_match is None and name.endswith(": unfamiliar"):
            pass2_match = name

    if pass1_match:
        logger.info(
            f"khan_course_overview_resolver: pass1 (Up-next callout) → {pass1_match!r}"
        )
        return pass1_match
    if pass2_match:
        logger.info(
            f"khan_course_overview_resolver: pass2 (first unfamiliar) → {pass2_match!r}"
        )
        return pass2_match

    logger.info("khan_course_overview_resolver: no target found")
    return None


# Per Jesse 2026-05-19: nav screens KEEP the LLM in the loop — they are too
# complex to eliminate the LLM yet. Resolvers stay registered for future
# variants (e.g. simple Cancel-button dismissals) but NAVIGATION_COURSE_OVERVIEW
# routes through the worker + LLM navigate path with tighter picking rules.
VARIANT_RESOLVERS: dict[str, Callable[[dict], Optional[str]]] = {
    # "NAVIGATION_COURSE_OVERVIEW": resolve_khan_course_overview_target,  # disabled — LLM nav path preferred
}


def build_click_bt(target: str, role: str = "AXLink", post_delay: float = 3.5) -> dict:
    """Build the canonical 1-action click BT for a resolver-picked target."""
    return {
        "type": "sequence",
        "children": [
            {
                "type": "action",
                "action": "find_and_click",
                "params": {
                    "target": target,
                    "role": role,
                    "strategy": "mouse_click",
                    "match_mode": "exact",
                    "post_delay": post_delay,
                },
            },
        ],
    }


def resolve(variant: str, tree: dict) -> Optional[str]:
    """Public entry point. Returns target string or None if no resolver / no match."""
    fn = VARIANT_RESOLVERS.get(variant)
    if not fn:
        return None
    try:
        return fn(tree)
    except Exception:
        logger.exception(f"deterministic_resolvers: {variant} resolver crashed")
        return None
