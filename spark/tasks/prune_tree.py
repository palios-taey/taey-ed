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

# Coordinate / id fields the LLM never uses (Mac executes by name+role).
_DROP_FIELDS = {"element_id", "position", "size", "visible_bbox"}

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
    collapsed.
    """
    return _prune_node(tree)


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


def _prune_node(node: dict) -> dict:
    """Prune a single node recursively, collapsing contentless wrappers among
    its children."""
    pruned = {}

    for key, val in node.items():
        if key in _DROP_FIELDS:
            continue

        if key == "children" and isinstance(val, list):
            collapsed: list = []
            for child in val:
                if not isinstance(child, dict):
                    continue
                pruned_child = _prune_node(child)
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
