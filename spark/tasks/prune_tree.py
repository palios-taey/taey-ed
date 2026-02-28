"""
Prune accessibility tree for Gemini prompts.

Strips fields Gemini doesn't use (coordinates, element_id, computed name)
and removes empty string values. Preserves everything Gemini needs for
BT building: role, title, description, value, children.

Platform-agnostic — capture_tree.py produces identical field sets regardless
of platform. The pruning rules are universal.
"""

import logging

logger = logging.getLogger("taey-ed")

# Fields Gemini never uses in BT building.
# - element_id: "for visual mapping only — Mac executes by name+role, NOT element_id"
# - name: always computed as title || description (redundant)
# - position: Gemini never generates coordinate-based actions
# - size: same
# - visible_bbox: position + size combined (same)
_DROP_FIELDS = {"element_id", "name", "position", "size", "visible_bbox"}


def prune_tree_for_prompt(tree: dict) -> dict:
    """
    Return a pruned copy of the tree for Gemini prompt inclusion.

    Removes:
      - element_id, name, position, size, visible_bbox from every node
      - Keys with empty string values (e.g., "title": "")

    Preserves:
      - role, title, description, value, children (everything Gemini needs)

    The original tree is NOT modified — returns a new dict.
    """
    return _prune_node(tree)


def _prune_node(node: dict) -> dict:
    """Prune a single node recursively."""
    pruned = {}

    for key, val in node.items():
        # Skip dropped fields
        if key in _DROP_FIELDS:
            continue

        # Recurse into children
        if key == "children" and isinstance(val, list):
            pruned_children = [_prune_node(child) for child in val]
            if pruned_children:
                pruned["children"] = pruned_children
            continue

        # Skip empty string values
        if isinstance(val, str) and val == "":
            continue

        pruned[key] = val

    return pruned
