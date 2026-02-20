# STATUS: FROZEN - Proven in v7. Verified 2026-02-19. Do not modify.
"""
Layer 1: Skeleton Extractor

Takes a Mac accessibility tree and strips it to structure only.
The skeleton is a deterministic string representation that's the same
for "the same screen with different question text."

KEEP: role, depth, sibling index, relative vertical position (top/mid/bottom)
DROP: actual text content, exact coordinates, transient states, element_id

The skeleton gets embedded into a 4096-dim vector for semantic matching
against Weaviate's ScreenEmbedding collection.

Design principle: Two exercise pages with different questions should produce
the SAME skeleton. A video page and an exercise page should produce
DIFFERENT skeletons.
"""

import hashlib
from typing import Optional


def extract_skeleton(tree: dict, viewport_height: int = 900) -> str:
    """
    Extract structural skeleton from accessibility tree.

    Args:
        tree: Nested dict from Mac's capture_tree (role, name, children, position, size)
        viewport_height: Screen height for vertical third calculation (default Mac viewport)

    Returns:
        Deterministic string representation of tree structure.
        Same screen type with different content → same skeleton.
    """
    lines = []
    _walk(tree, depth=0, sibling_idx=0, viewport_height=viewport_height, lines=lines)
    return "\n".join(lines)


def skeleton_hash(skeleton: str) -> str:
    """SHA256 hash of skeleton string, first 16 chars. For dedup."""
    return hashlib.sha256(skeleton.encode()).hexdigest()[:16]


def extract_dynamic_text(tree: dict) -> list[str]:
    """
    Extract the dynamic text content from a tree — the stuff the skeleton drops.
    Used by the Router's ISOMORPHIC path to send only the changing parts to the LLM.

    Returns list of text values from leaf-ish text nodes.
    """
    texts = []
    _collect_text(tree, texts)
    return texts


# --- Internal ---

# Roles that carry structural meaning (define screen type)
STRUCTURAL_ROLES = {
    "AXWebArea", "AXGroup", "AXList", "AXTable", "AXRow",
    "AXColumn", "AXSection", "AXArticle", "AXForm", "AXToolbar",
    "AXTabGroup", "AXScrollArea", "AXSplitGroup", "AXLayoutArea",
    "AXLandmarkMain", "AXLandmarkNavigation", "AXLandmarkBanner",
    "AXLandmarkContentInfo", "AXLandmarkSearch",
}

# Roles that define interactive elements (structural but leaf-like)
INTERACTIVE_ROLES = {
    "AXButton", "AXCheckBox", "AXRadioButton", "AXTextField",
    "AXTextArea", "AXComboBox", "AXPopUpButton", "AXSlider",
    "AXLink", "AXMenuItem", "AXMenuButton", "AXDisclosureTriangle",
    "AXIncrementor",
}

# Roles whose COUNT matters but individual text doesn't
COUNT_ROLES = {
    "AXStaticText", "AXImage", "AXHeading",
}

# Roles to skip entirely (noise)
SKIP_ROLES = {
    "AXUnknown", "AXValueIndicator", "AXRuler", "AXRulerMarker",
    "AXGrowArea", "AXMatte", "AXSystemWide",
}


def _vertical_third(position: Optional[list], viewport_height: int) -> str:
    """Classify Y position into top/mid/bot third."""
    if not position or len(position) < 2:
        return "?"
    y = position[1]
    third = viewport_height // 3
    if y < third:
        return "T"
    elif y < third * 2:
        return "M"
    else:
        return "B"


def _walk(
    node: dict,
    depth: int,
    sibling_idx: int,
    viewport_height: int,
    lines: list[str],
    max_depth: int = 15,
):
    """
    Recursive tree walk producing skeleton lines.

    Each line format: {indent}{role}[{sibling_idx}]@{vertical_third}
    For COUNT_ROLES, we collapse to: {indent}{role}x{count}

    Depth is capped at max_depth to keep skeletons manageable.
    """
    if depth > max_depth:
        return

    role = node.get("role", "")

    if role in SKIP_ROLES:
        return

    position = node.get("position")
    children = node.get("children", [])
    indent = "  " * depth
    vt = _vertical_third(position, viewport_height)

    if role in STRUCTURAL_ROLES:
        # Structural container: emit role + position, recurse into children
        lines.append(f"{indent}{role}[{sibling_idx}]@{vt}")
        _walk_children(children, depth + 1, viewport_height, lines, max_depth)

    elif role in INTERACTIVE_ROLES:
        # Interactive leaf: emit role + position, count but don't show text
        child_count = len(children)
        if child_count > 0:
            lines.append(f"{indent}{role}[{sibling_idx}]@{vt}+{child_count}")
        else:
            lines.append(f"{indent}{role}[{sibling_idx}]@{vt}")

    elif role in COUNT_ROLES:
        # Text/image: just counted at parent level, don't emit individual lines
        # (handled by parent via _count_children)
        pass

    elif role == "AXApplication" or role == "AXWindow":
        # Top-level containers: always traverse
        lines.append(f"{indent}{role}")
        _walk_children(children, depth + 1, viewport_height, lines, max_depth)

    else:
        # Unknown role: treat as structural if it has children, skip if leaf
        if children:
            lines.append(f"{indent}{role}[{sibling_idx}]@{vt}")
            _walk_children(children, depth + 1, viewport_height, lines, max_depth)


def _walk_children(
    children: list[dict],
    depth: int,
    viewport_height: int,
    lines: list[str],
    max_depth: int,
):
    """
    Walk children, but collapse COUNT_ROLES into summary counts.
    """
    # First pass: count text/image nodes
    counts: dict[str, int] = {}
    structural_children = []

    for i, child in enumerate(children):
        role = child.get("role", "")
        if role in COUNT_ROLES:
            counts[role] = counts.get(role, 0) + 1
        elif role not in SKIP_ROLES:
            structural_children.append((i, child))

    # Emit count summaries
    indent = "  " * depth
    for role, count in sorted(counts.items()):
        lines.append(f"{indent}{role}x{count}")

    # Recurse into structural/interactive children
    for i, child in structural_children:
        _walk(child, depth, i, viewport_height, lines, max_depth)


def _collect_text(node: dict, texts: list[str]):
    """Collect actual text values from tree for dynamic text extraction."""
    role = node.get("role", "")

    if role in ("AXStaticText", "AXHeading"):
        # Get text from value, name, title, or description
        for field in ("value", "name", "title", "description"):
            val = node.get(field, "")
            if val and val.strip():
                texts.append(val.strip())
                break

    for child in node.get("children", []):
        _collect_text(child, texts)
