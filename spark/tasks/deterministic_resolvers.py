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
import hashlib
import json
import re
from dataclasses import asdict, dataclass
from typing import Any, Callable, Optional

logger = logging.getLogger(__name__)

UNIT_BANNER_RE = re.compile(r"^Unit \d+ Up next for you!$", re.IGNORECASE)
SUMMARY_ADVANCE_BUTTONS = ("next question", "show summary")
INTRO_ADVANCE_BUTTONS = ("lets go", "start quiz", "start unit test")
STABILITY_REQUIRED_MATCHES = 2
STABILITY_MAX_POLLS = 6
_STABILITY_STATE: dict[str, dict[str, Any]] = {}


@dataclass(frozen=True)
class ScopedAddress:
    role: str
    target: str
    match_mode: str
    source_field: str
    scope: str = ""


@dataclass(frozen=True)
class StabilityObservation:
    key: str
    stable: bool
    exhausted: bool
    stable_count: int
    poll_count: int
    signature: str


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


def _walk_nodes_dom_order(tree: dict):
    stack = [tree]
    while stack:
        node = stack.pop()
        if isinstance(node, dict):
            yield node
            children = node.get("children") or []
            for child in reversed(children):
                stack.append(child)


def _is_visible(bbox) -> bool:
    """A bbox is visible if both width and height are > 0."""
    if not bbox or len(bbox) < 4:
        return False
    return bbox[2] > 0 and bbox[3] > 0


def _text_values(node: dict) -> list[str]:
    values: list[str] = []
    for key in ("name", "description", "value"):
        value = str(node.get(key) or "").strip()
        if value:
            values.append(value)
    return values


def _normalize_label(value: str) -> str:
    normalized = re.sub(r"\s+", " ", str(value or "").strip()).casefold()
    return normalized.replace("’", "'")


def _button_label_key(value: str) -> str:
    return _normalize_label(value).replace("'", "")


def _stable_node(node: Any) -> Any:
    if not isinstance(node, dict):
        return None
    stable = {}
    for key in (
        "role",
        "name",
        "description",
        "value",
        "visible_bbox",
        "position",
        "size",
        "element_id",
    ):
        value = node.get(key)
        if value not in (None, "", [], {}):
            stable[key] = value
    children = [
        child
        for child in (_stable_node(item) for item in (node.get("children") or []))
        if child is not None
    ]
    if children:
        stable["children"] = children
    return stable


def _digest(payload: Any) -> str:
    body = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def tree_stability_digest(tree: dict) -> str:
    return _digest(_stable_node(tree))


def observe_wait_until_stable(
    *,
    key: str,
    tree: dict,
    required_matches: int = STABILITY_REQUIRED_MATCHES,
    max_polls: int = STABILITY_MAX_POLLS,
) -> StabilityObservation:
    """Compare successive posted AX-tree snapshots only.

    The frozen Mac bundle does not round-trip BT blackboard or find_all
    telemetry, so this engine primitive can only depend on the tree already
    posted to /next_action each poll.
    """
    signature = tree_stability_digest(tree)
    prior = _STABILITY_STATE.get(key) or {}
    same = prior.get("signature") == signature
    stable_count = int(prior.get("stable_count", 0)) + 1 if same else 1
    poll_count = int(prior.get("poll_count", 0)) + 1
    required = max(1, int(required_matches))
    limit = max(required, int(max_polls))
    stable = stable_count >= required
    exhausted = poll_count >= limit and not stable
    observation = StabilityObservation(
        key=key,
        stable=stable,
        exhausted=exhausted,
        stable_count=stable_count,
        poll_count=poll_count,
        signature=signature,
    )
    if stable or exhausted:
        _STABILITY_STATE.pop(key, None)
    else:
        _STABILITY_STATE[key] = {
            "signature": signature,
            "stable_count": stable_count,
            "poll_count": poll_count,
        }
    return observation


def build_wait_until_stable_directive(
    observation: StabilityObservation,
    *,
    seconds: float = 1.0,
) -> dict | None:
    if observation.stable or observation.exhausted:
        return None
    return {
        "directive": "wait",
        "reason": "engine_wait_until_stable",
        "seconds": max(0.5, float(seconds)),
        "stability": asdict(observation),
    }


def _node_matches_text(node: dict, text: str | None, contains: bool = False) -> bool:
    if not text:
        return True
    needle = _normalize_label(text)
    for value in _text_values(node):
        haystack = _normalize_label(value)
        if (contains and needle in haystack) or (not contains and needle == haystack):
            return True
    return False


def _scope_nodes(tree: dict, role: str | None, text: str | None) -> list[tuple[str, dict]]:
    if not role and not text:
        return [("", tree)]
    scopes = []
    for node in _walk_nodes_dom_order(tree):
        if role and node.get("role") != role:
            continue
        if not _node_matches_text(node, text, contains=True):
            continue
        label = " / ".join(_text_values(node)[:2]) or str(node.get("role") or "")
        scopes.append((label, node))
    return scopes


def resolve_scoped_address(
    tree: dict,
    *,
    role: str,
    target: str | None = None,
    contains: str | None = None,
    scope_role: str | None = None,
    scope_text: str | None = None,
    index: int = 0,
) -> ScopedAddress | None:
    candidates: list[ScopedAddress] = []
    for scope_label, scope in _scope_nodes(tree, scope_role, scope_text):
        for node in _walk_nodes_dom_order(scope):
            if node.get("role") != role:
                continue
            if target and not _node_matches_text(node, target, contains=False):
                continue
            if contains and not _node_matches_text(node, contains, contains=True):
                continue
            for field in ("name", "description", "value"):
                value = str(node.get(field) or "").strip()
                if value:
                    candidates.append(
                        ScopedAddress(
                            role=role,
                            target=value,
                            match_mode="exact",
                            source_field=field,
                            scope=scope_label,
                        )
                    )
                    break
    if not candidates:
        return None
    if index < 0 or index >= len(candidates):
        return None
    return candidates[index]


def build_scoped_click_bt(address: ScopedAddress, *, post_delay: float = 2.5) -> dict:
    return build_click_bt(
        address.target,
        role=address.role,
        post_delay=post_delay,
    )


def _find_transition_button(tree: dict, allowed: tuple[str, ...]) -> str | None:
    matches: dict[str, str] = {}
    for node in _walk_nodes_dom_order(tree):
        if node.get("role") != "AXButton":
            continue
        for value in _text_values(node):
            key = _button_label_key(value)
            if key in allowed and key not in matches:
                matches[key] = value
    for key in allowed:
        if key in matches:
            return matches[key]
    return None


def _has_upnext_link(tree: dict) -> bool:
    for node in _walk_nodes_dom_order(tree):
        if node.get("role") != "AXLink":
            continue
        for value in _text_values(node):
            if "up next" in _normalize_label(value):
                return True
    return False


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


def build_transition_bt(variant: str, tree: dict) -> dict | None:
    target = _find_transition_button(tree, SUMMARY_ADVANCE_BUTTONS)
    if target:
        logger.info(
            "transition_resolver: %s -> AXButton %r",
            variant,
            target,
        )
        return build_click_bt(target, role="AXButton", post_delay=2.5)

    if _has_upnext_link(tree):
        logger.info("transition_resolver: %s -> AXLink description_contains 'Up next'", variant)
        return {
            "type": "sequence",
            "name": "transition_upnext",
            "children": [
                {
                    "type": "action",
                    "action": "find_all",
                    "params": {"role": "AXLink", "description_contains": "Up next"},
                    "store": "upnext",
                },
                {
                    "type": "action",
                    "action": "click",
                    "params": {"element": "$upnext.0.element", "strategy": "mouse_click"},
                },
                {"type": "action", "action": "wait", "params": {"seconds": 3.0}},
            ],
        }

    target = _find_transition_button(tree, INTRO_ADVANCE_BUTTONS)
    if target:
        logger.info(
            "transition_resolver: %s -> intro AXButton %r",
            variant,
            target,
        )
        return build_click_bt(target, role="AXButton", post_delay=3.0)

    logger.warning("transition_resolver: %s has no live forward control", variant)
    return None


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
