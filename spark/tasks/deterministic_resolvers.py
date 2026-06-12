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


# ──────────────────────────────────────────────────────────────────────
# Claude-primary-as-worker track (Jesse 2026-05-19)
# ──────────────────────────────────────────────────────────────────────
# When the solution is KNOWN (claude-primary already understands the screen)
# but the worker CAN'T EXECUTE it correctly (observed empirically on Khan's
# interactive_graph widget — worker emits no-op `wait` BTs or BTs that only
# move 1 of 4 points), route to claude-primary as the BT builder directly.
# No more notes-to-worker hoping the worker complies — claude builds the BT
# itself via vision + math, returns it as a fully-unrolled action sequence.
#
# Variants that ALWAYS route to claude-primary-as-worker (skip the LLM worker):
CLAUDE_PRIMARY_WORKER_VARIANTS = {
    "EXERCISE_GRAPH_DRAG_PLOT",
    "EXERCISE_GRAPH_DRAG_WRONG_RETRY",
    "EXERCISE_GRAPH_DRAG_PARTIAL",
}


GRAPH_DRAG_VISION_PROMPT = """You are analyzing a Khan Academy graph-plot exercise screenshot. The learner must drag N labeled points (e.g. "Point 1", "Point 2", ...) onto specific coordinates on a graph.

YOUR TASK — produce a JSON object that lets the automation move each point to the correct position via OS-level keyboard events. Read CAREFULLY from the screenshot:

1. Identify the EXERCISE QUESTION and DATA TABLE on screen — these tell you which (x, y) data points must be plotted.
2. Identify the GRAPH axes — read the X-axis label (e.g. "mass (kg)") and Y-axis label (e.g. "kinetic energy (J)"), plus the visible tick values on each axis.
3. Determine the TARGET data coordinates for each Point N — there will typically be 4 points to plot, and the question filters which rows from the data table should be plotted.

Output ONLY a JSON object with this exact shape (no other text, no markdown fences):

{
  "snap_step_x": <float — the smallest x-axis tick interval visible, e.g. 0.5 or 1.0>,
  "snap_step_y": <float — same for y-axis>,
  "graph_pixel_bbox": [<gx0>, <gy0>, <gx1>, <gy1>],
  "current_to_target": [
    {
      "point_n": 1,
      "current_data": [<cur_x>, <cur_y>],
      "target_data": [<tgt_x>, <tgt_y>],
      "visible_pixel_now": [<px>, <py>]
    },
    ...
  ]
}

Notes:
- visible_pixel_now is the pixel position of the point's current visible marker on the graph (NOT the offscreen AXButton sentinel). For points still at (0,0) it's the graph's origin pixel. Read this from the screenshot.
- current_data is the point's current data coordinates (read from the AXButton name 'Point N at <x> comma <y>' which is provided in the AX context).
- snap_step values are what one ArrowRight or ArrowUp press should move (1/50 of axis span by default, but Khan varies — infer from the visible tick spacing).
"""


def _parse_point_axbuttons(tree: dict) -> list:
    """Walk the AX tree, extract Point N AXButtons with their current (x_data, y_data)
    parsed from name = 'Point N at <x> comma <y>.'.
    Returns list of {point_n, current_x, current_y} sorted by point_n.
    """
    import re
    pattern = re.compile(r"^Point\s+(\d+)\s+at\s+(-?\d+(?:\.\d+)?)\s+comma\s+(-?\d+(?:\.\d+)?)\.")
    points = []
    stack = [tree]
    while stack:
        n = stack.pop()
        if not isinstance(n, dict):
            continue
        if n.get("role") == "AXButton":
            name = (n.get("name") or "").strip()
            m = pattern.match(name)
            if m:
                points.append({
                    "point_n": int(m.group(1)),
                    "current_x": float(m.group(2)),
                    "current_y": float(m.group(3)),
                })
        for c in (n.get("children") or []):
            stack.append(c)
    # Deduplicate by point_n (keep first occurrence)
    seen = set()
    unique = []
    for p in points:
        if p["point_n"] in seen:
            continue
        seen.add(p["point_n"])
        unique.append(p)
    unique.sort(key=lambda p: p["point_n"])
    return unique


async def compute_graph_drag_bt(tree: dict, screenshot_b64: str) -> Optional[dict]:
    """Claude-primary acts as the BT builder for graph-drag exercises.

    Reads the AX tree for current Point positions, calls Claude vision on the
    screenshot to extract graph dimensions + target coordinates + snap step,
    computes the full click_at + press_key sequence per point, returns a
    fully-unrolled BT.

    Returns None on any failure (caller falls back to worker path).
    """
    import json as _json
    points_ax = _parse_point_axbuttons(tree)
    if not points_ax:
        logger.warning("compute_graph_drag_bt: no Point N AXButtons found in tree")
        return None

    ax_summary = "\n".join(
        f"  Point {p['point_n']} currently at ({p['current_x']}, {p['current_y']})"
        for p in points_ax
    )
    full_prompt = (
        GRAPH_DRAG_VISION_PROMPT
        + "\n\nCURRENT POINT POSITIONS FROM AX TREE (use as `current_data`):\n"
        + ax_summary
    )

    try:
        from spark.tasks.call_gemini import _solve_with_claude_cli_image
        raw = await _solve_with_claude_cli_image(full_prompt, screenshot_b64)
    except Exception:
        logger.exception("compute_graph_drag_bt: vision call failed")
        return None

    if not raw:
        logger.warning("compute_graph_drag_bt: empty vision response")
        return None

    # Extract JSON from response (model may wrap in fences)
    raw = raw.strip()
    if raw.startswith("```"):
        # strip markdown fence
        raw = raw.split("```", 2)[1]
        if raw.startswith("json"):
            raw = raw[4:]
        raw = raw.strip()
    try:
        plan = _json.loads(raw)
    except Exception:
        logger.exception(f"compute_graph_drag_bt: failed to parse plan JSON: {raw[:300]!r}")
        return None

    logger.info(f"compute_graph_drag_bt: vision plan: {_json.dumps(plan, indent=2)[:1500]}")

    # Points-vs-pixels Retina conversion. macOS handlers (click_at) use POINTS
    # (same space as AX visible_bbox); the screenshot is in PIXELS. On Retina
    # displays pixels = 2 * points. If vision returned graph_pixel_bbox with
    # any coordinate > 1500, treat it as Retina pixels and halve everything.
    bbox_max = max(plan.get("graph_pixel_bbox") or [0])
    is_retina = bbox_max > 1500
    if is_retina:
        scale = 0.5
        logger.info(f"compute_graph_drag_bt: Retina scale detected (max bbox={bbox_max}) — halving coords")
    else:
        scale = 1.0

    step_x = float(plan.get("snap_step_x") or 0)
    step_y = float(plan.get("snap_step_y") or 0)
    moves = plan.get("current_to_target") or []
    if not (step_x > 0 and step_y > 0 and moves):
        logger.warning(f"compute_graph_drag_bt: invalid plan: {plan}")
        return None

    # Persist the answer for the user as a fallback when BT execution fails.
    # _escalate_to_claude_diagnosing reads this and surfaces user_input_needed
    # with the answer text so the user can manually finish the exercise.
    try:
        from pathlib import Path as _P
        # We don't have the screen hash here directly — caller will pass via
        # a side channel. For now, write a generic latest-plan file the
        # escalator can find by mtime if needed.
        _P("/tmp/taey-ed-graph-drag-last-plan.json").write_text(_json.dumps(plan, indent=2))
    except Exception:
        pass

    # Build the BT: per point, click_at(visible_pixel_now) + arrow presses, then Check at end.
    children = []
    for move in moves:
        cur = move.get("current_data") or [0, 0]
        tgt = move.get("target_data") or [0, 0]
        vis = move.get("visible_pixel_now") or [0, 0]
        try:
            dx_data = float(tgt[0]) - float(cur[0])
            dy_data = float(tgt[1]) - float(cur[1])
        except Exception:
            continue
        if dx_data == 0 and dy_data == 0:
            continue  # already correct
        n_right = int(round(dx_data / step_x)) if dx_data > 0 else 0
        n_left = int(round(-dx_data / step_x)) if dx_data < 0 else 0
        n_up = int(round(dy_data / step_y)) if dy_data > 0 else 0
        n_down = int(round(-dy_data / step_y)) if dy_data < 0 else 0

        # Focus this point: click_at its visible-pixel center.
        # Apply Retina scale conversion (pixels → points).
        try:
            px = int(round(float(vis[0]) * scale))
            py = int(round(float(vis[1]) * scale))
        except Exception:
            continue
        children.append({
            "type": "action",
            "action": "click_at",
            "params": {"x": px, "y": py, "post_delay": 0.4},
        })
        # Issue arrow presses, x then y.
        for _ in range(n_right):
            children.append({"type": "action", "action": "press_key",
                             "params": {"key": "Right", "post_delay": 0.08}})
        for _ in range(n_left):
            children.append({"type": "action", "action": "press_key",
                             "params": {"key": "Left", "post_delay": 0.08}})
        for _ in range(n_up):
            children.append({"type": "action", "action": "press_key",
                             "params": {"key": "Up", "post_delay": 0.08}})
        for _ in range(n_down):
            children.append({"type": "action", "action": "press_key",
                             "params": {"key": "Down", "post_delay": 0.08}})
        children.append({"type": "action", "action": "wait",
                         "params": {"seconds": 0.4}})

    if not children:
        logger.info("compute_graph_drag_bt: nothing to move — all points already at target")
        return None

    logger.info(
        f"compute_graph_drag_bt: built BT with {len(children)} actions for {len(moves)} moves"
    )

    # Finish with Check.
    children.append({
        "type": "action",
        "action": "wait_for_element",
        "params": {"role": "AXButton", "target": "Check", "max_wait": 3.0},
    })
    children.append({
        "type": "action",
        "action": "find_and_click",
        "params": {
            "target": "Check", "role": "AXButton",
            "strategy": "mouse_click", "match_mode": "exact", "post_delay": 2.5,
        },
    })
    return {"type": "sequence", "children": children}


def compute_graph_drag_bt_sync(tree: dict, screenshot_b64: str) -> Optional[dict]:
    """Sync wrapper around compute_graph_drag_bt for use from sync request handlers."""
    import asyncio
    try:
        return asyncio.run(compute_graph_drag_bt(tree, screenshot_b64))
    except RuntimeError:
        # Already inside an event loop — fall back to creating a task on a new loop
        try:
            loop = asyncio.new_event_loop()
            try:
                return loop.run_until_complete(compute_graph_drag_bt(tree, screenshot_b64))
            finally:
                loop.close()
        except Exception:
            logger.exception("compute_graph_drag_bt_sync: event-loop fallback failed")
            return None
    except Exception:
        logger.exception("compute_graph_drag_bt_sync failed")
        return None


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
