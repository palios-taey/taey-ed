"""
Filter an accessibility tree down to the relevant info for LLM prompts.

Two problems with sending the raw tree (Jesse 2026-06-13): it is huge (Khan
trees run ~450KB / ~110K tokens — they blow the classifier's cost budget, which
errors out and silently degrades to screen_type=UNKNOWN) AND, paradoxically, it
used to drop `name` — which is exactly where the content-bearing labels live
("Select an answer", "Check", choice text, headings). So the prompt was both
bloated and content-poor.

This filter keeps only the relevant info:
  - KEEP role + name/title/description/value (the labels + text the LLM reads)
    + a few answer-widget state fields (value/selected/enabled).
  - DROP coordinates / ids (element_id, position, size, visible_bbox) — the Mac
    resolves actions by name+role, never coordinates.
  - COLLAPSE contentless structural wrappers (AXGroup/AXGeneric/... with no
    name/title/description/value) by hoisting their children. Khan trees are
    ~50% empty wrapper groups; flattening them is the bulk of the size win and
    loses nothing the LLM needs.

The original tree is NOT modified — returns a new dict. NOT truncation: no
content is dropped, only coordinate noise and empty structural nesting.

Platform-agnostic — capture_tree.py produces identical field sets regardless
of platform.
"""

import logging

logger = logging.getLogger("taey-ed")

# Id / redundant-coordinate fields the LLM never uses. visible_bbox is KEPT:
# the dropdown solver distinguishes identical "Select an answer" comboboxes by
# POSITION (bbox-center click_at) + preceding label — dropping it (my 2026-06-13
# mistake) forced by-name targeting, which can't tell the boxes apart. position
# and size are redundant with visible_bbox.
_DROP_FIELDS = {"element_id", "position", "size"}

# CLASSIFIER-ONLY tight allowlist (Jesse 2026-06-15, "something very broken").
# The classifier path (prune_tree_for_prompt) is a DENYLIST that leaked ~27
# AX-internal noise fields per node (startTextMarker, endTextMarker,
# selectedTextRange, visibleCharacterRange, frame, visible_bbox, ChromeAXNodeId,
# dOMIdentifier, insertionPointLineNumber, numberOfCharacters, ...). On a Khan
# sorter that bloated 119 nodes / 3.3KB of real content to 138KB / ~34.5K tokens
# -> the classifier STARVED its cost budget -> silent screen_type=UNKNOWN ->
# worker freelance. The classifier reads role+name+structure (never coordinates),
# so it gets ONLY these fields. An allowlist cannot be bypassed by a new noise
# field. This is CLASSIFICATION-only — the WORKER path (filter_tree_base) keeps
# the denylist because its solver needs visible_bbox for click_at/drag.
_CLASSIFIER_KEEP_FIELDS = {
    "role",            # the structural signal
    "name", "title", "description", "value",  # content: questions, labels, choice text
    "dOMClassList",    # widget identity (e.g. 'perseus-sortable') — strong classify signal
    "roleDescription", # e.g. 'image' / 'button' — small, useful
    "selected", "enabled",  # answer-widget state
}

# Fields whose non-empty presence means a node carries content worth keeping.
_CONTENT_FIELDS = ("name", "title", "description", "value")

# Roles that are pure structural containers — collapse them when they carry no
# content of their own (hoist their children into the parent).
_STRUCTURAL_ROLES = {
    "AXGroup", "AXGeneric", "AXScrollArea", "AXSplitGroup", "AXSplitter",
    "AXLayoutArea", "AXLayoutItem", "AXUnknown", "AXEmptyGroup",
}


def _has_content(node: dict) -> bool:
    for key in _CONTENT_FIELDS:
        if str(node.get(key) or "").strip():
            return True
    return False


def prune_tree_for_prompt(tree: dict) -> dict:
    """Return a filtered copy of the tree for LLM prompt inclusion.

    Keeps content (role + name/title/description/value), drops coordinate noise,
    and collapses contentless structural wrappers. The root node itself is never
    collapsed. Uses the CLASSIFIER tight allowlist — coordinates and AX-internal
    noise are not needed to classify (the LLM reads role+name+structure).
    """
    return _prune_node(tree, keep_only=_CLASSIFIER_KEEP_FIELDS)


def filter_tree_base(tree: dict) -> dict:
    """THE default base filter for EVERY tree sent to an LLM — classifier AND
    worker. Two-stage design (Jesse, discussed many times):

      1. Base (here, always): scope to the page content (AXWebArea), dropping
         ALL browser chrome (toolbar, tabs, extensions, address bar, "View
         progress"/"Share"/"Tab Search" popups...), then keep content + collapse
         empty structural wrappers + drop coordinate noise.
      2. Per-screen: each screen-type YAML narrows further to just that screen's
         relevant elements.

    Chrome chrome is NEVER relevant to solving a course screen and is pure
    bloat/confusion. This is filtering, NOT truncation — no content is dropped,
    and (unlike the old _sanitize_tree_for_worker) values are never clipped.
    """
    from spark.tasks.prompt_codex import _find_web_area

    web = _find_web_area(tree) or tree
    return _prune_node(web)


def _prune_node(node: dict, keep_only: set | None = None) -> dict:
    """Prune a single node recursively, collapsing contentless wrappers among
    its children. If keep_only is given, keep ONLY those fields (allowlist, used
    by the classifier path); otherwise drop _DROP_FIELDS (denylist, worker path
    which still needs visible_bbox for click_at/drag)."""
    pruned = {}

    for key, val in node.items():
        if keep_only is not None:
            if key not in keep_only and key != "children":
                continue
        elif key in _DROP_FIELDS:
            continue

        if key == "children" and isinstance(val, list):
            collapsed: list = []
            for child in val:
                if not isinstance(child, dict):
                    continue
                pruned_child = _prune_node(child, keep_only=keep_only)
                # Collapse a contentless structural wrapper: splice its (already
                # pruned) children in place of the wrapper itself.
                if (
                    child.get("role") in _STRUCTURAL_ROLES
                    and not _has_content(child)
                ):
                    collapsed.extend(pruned_child.get("children", []))
                else:
                    collapsed.append(pruned_child)
            if collapsed:
                pruned["children"] = collapsed
            continue

        if isinstance(val, str) and val == "":
            continue

        pruned[key] = val

    return pruned
