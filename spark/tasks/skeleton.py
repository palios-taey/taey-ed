"""
Layer 1: Skeleton Extractor

Takes a Mac accessibility tree and strips it to structure only.
The skeleton is a deterministic string representation that's the same
for "the same screen with different question text."

KEEP: role, depth, sibling index, relative vertical position (top/mid/bottom)
DROP: actual text content, exact coordinates, transient states, element_id

V21: Added web_content_only scoping. When True (default), skeleton is
computed from the AXWebArea subtree only, excluding browser chrome
(menus, bookmarks, tabs). This ensures the hash reflects page content
structure, not browser state.

Design principle: Two exercise pages with different questions should produce
the SAME skeleton. A video page and an exercise page should produce
DIFFERENT skeletons.
"""

import hashlib
from typing import Optional


def extract_skeleton(tree: dict, viewport_height: int = 900,
                     web_content_only: bool = True) -> str:
    """
    Extract structural skeleton from accessibility tree.

    Args:
        tree: Nested dict from Mac's capture_tree (role, name, children, position, size)
        viewport_height: Screen height for vertical third calculation (default Mac viewport)
        web_content_only: If True, scope to AXWebArea subtree (excludes browser chrome).
                         Default True for V21 — hashes should reflect page content, not
                         browser menus/bookmarks/tabs.

    Returns:
        Deterministic string representation of tree structure.
        Same screen type with different content → same skeleton.
    """
    root = tree
    if web_content_only:
        web_area = _find_web_area(tree)
        if web_area:
            root = web_area

    lines = []
    # ANSWER-WIDGET SIGNATURE (depth-independent) — prepended so it is part of
    # the hash. Root-cause fix (2026-06-15, operator-confirmed): Khan/Perseus
    # wraps answer widgets 13–25 AXGroup layers deep, past _walk's max_depth=15,
    # so the structural walk DROPPED them — every exercise sub-type (dropdown,
    # text-input, multiple-choice, matcher, numeric) collapsed to the SAME
    # skeleton -> ONE hash (8646957) mapped to SIX screen types -> the worker
    # correctly re-read the live widget, re-classified, and conformance rejected
    # it as "worker changed screen_type" -> deadlock/terminal thrash. The
    # presence-set of answer-input roles, collected over the FULL web area with
    # no depth cap, is the discriminating feature; it separates the sub-types
    # without fragmenting within one (count is ignored on purpose). This
    # CORRECTS the data shape upstream so each sub-type recognizes to its own
    # hash — it does not add a bypass.
    lines.append(_answer_widget_signature(root))
    _walk(root, depth=0, sibling_idx=0, viewport_height=viewport_height, lines=lines)
    return "\n".join(lines)


def skeleton_hash(skeleton: str) -> str:
    """SHA256 hash of skeleton string, first 16 chars. For dedup."""
    return hashlib.sha256(skeleton.encode()).hexdigest()[:16]


# Interactive roles that constitute an ANSWER to an exercise — the feature that
# distinguishes exercise sub-types (dropdown vs text-input vs choice vs matcher).
# Distinct from chrome controls (tab strip, Share, Extensions), which live above
# the AXWebArea and are already excluded by web_content_only scoping.
ANSWER_WIDGET_ROLES = (
    "AXComboBox", "AXPopUpButton", "AXTextField", "AXTextArea",
    "AXCheckBox", "AXRadioButton", "AXSlider", "AXIncrementor",
)


def _answer_widget_signature(root: dict) -> str:
    """Depth-independent presence-set of answer-widget roles in the subtree.

    Walks the ENTIRE subtree (no max_depth cap — Perseus nests widgets far
    deeper than the structural walk reaches) and returns a deterministic
    `WIDGETS:role,role,...` line of the DISTINCT answer-widget roles present.
    Presence (not count) is intentional: it separates exercise sub-types
    without splitting one sub-type across blank-count variants.
    """
    present: set[str] = set()
    stack = [root]
    while stack:
        n = stack.pop()
        role = n.get("role", "")
        if role in ANSWER_WIDGET_ROLES:
            present.add(role)
        for child in n.get("children", []):
            stack.append(child)
    return "WIDGETS:" + ",".join(sorted(present))


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


def _find_web_area(node: dict) -> Optional[dict]:
    """
    Find the first AXWebArea node in the tree (BFS).
    AXWebArea is the root of browser page content — everything above it
    is browser chrome (menus, toolbar, bookmarks, tabs).
    """
    stack = [node]
    while stack:
        n = stack.pop(0)  # BFS — find shallowest AXWebArea
        if n.get("role") == "AXWebArea":
            return n
        for child in n.get("children", []):
            stack.append(child)
    return None


def extract_content_fingerprint(tree: dict) -> dict:
    """
    Extract structural fingerprint from content area only.
    Captures element counts and key button labels — NOT article text,
    question text, or other variable content.

    Used for V22 fingerprint matching. V21 logs these alongside Flash
    classifications to build training data.
    """
    web_area = _find_web_area(tree)
    if not web_area:
        return {}

    fp = {
        "radio_button_count": 0,
        "checkbox_count": 0,
        "text_field_count": 0,
        "text_area_count": 0,
        "slider_count": 0,
        "form_count": 0,
        "static_text_count": 0,
        "heading_count": 0,
        "link_count": 0,
        "image_count": 0,
        "button_labels": [],
        "has_video_player": False,
        "has_transcript": False,
        "has_sidebar_nav": False,
    }

    _walk_fingerprint(web_area, fp)

    # Deduplicate and sort button labels
    fp["button_labels"] = sorted(set(fp["button_labels"]))
    return fp


def _walk_fingerprint(node: dict, fp: dict):
    """Walk tree counting elements and collecting button labels."""
    role = node.get("role", "")

    # Count interactive elements
    if role == "AXRadioButton":
        fp["radio_button_count"] += 1
    elif role == "AXCheckBox":
        fp["checkbox_count"] += 1
    elif role == "AXTextField":
        fp["text_field_count"] += 1
    elif role == "AXTextArea":
        fp["text_area_count"] += 1
    elif role == "AXSlider":
        fp["slider_count"] += 1
    elif role == "AXForm":
        fp["form_count"] += 1
    elif role == "AXStaticText":
        fp["static_text_count"] += 1
    elif role == "AXHeading":
        fp["heading_count"] += 1
    elif role == "AXLink":
        fp["link_count"] += 1
    elif role == "AXImage":
        fp["image_count"] += 1
    elif role == "AXButton":
        # Collect button label — developer-set, stable across instances
        label = node.get("name") or node.get("title") or node.get("description") or ""
        if label.strip():
            fp["button_labels"].append(label.strip())
    elif role == "AXVideo":
        fp["has_video_player"] = True

    # Detect structural signals from names/descriptions
    name_lower = (node.get("name") or "").lower()
    if "transcript" in name_lower:
        fp["has_transcript"] = True
    if "outline" in name_lower or "sidebar" in name_lower:
        fp["has_sidebar_nav"] = True

    for child in node.get("children", []):
        _walk_fingerprint(child, fp)


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
