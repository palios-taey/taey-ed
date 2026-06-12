"""
Minimal tree-signal helpers shared by the classifier and diagnostics.

The old prompt assembler and handler-reference monolith were removed in
scr1-slash. Worker prompt assembly now lives only in screen_type_assembler.py.
"""

from __future__ import annotations

import logging
from collections import Counter

logger = logging.getLogger("taey-ed")

_CHROME_ROLES = {"AXMenuBar", "AXMenuBarItem", "AXMenu", "AXToolbar"}
_VIDEO_KEYWORDS = {
    "video player",
    "youtube",
    "play video",
    "vimeo player",
    "wistia",
    "pause video",
    "video playback",
    "playback speed",
    "video progress",
    "media player",
    "video timeline",
}


def _find_web_area(node: dict) -> dict:
    if not isinstance(node, dict):
        return node
    if node.get("role") == "AXWebArea":
        return node
    for child in node.get("children", []):
        result = _find_web_area(child)
        if isinstance(result, dict) and result.get("role") == "AXWebArea":
            return result
    return node


def _count_roles(node: dict) -> Counter:
    counts = Counter()

    def walk(n):
        if not isinstance(n, dict):
            return
        role = n.get("role", "")
        if role and role not in _CHROME_ROLES:
            counts[role] += 1
        for child in n.get("children", []):
            walk(child)

    walk(node)
    return counts


def _has_video_signals(node: dict) -> bool:
    if not isinstance(node, dict):
        return False
    for field in ("name", "title", "value", "description"):
        text = node.get(field, "")
        if text and isinstance(text, str):
            lower = text.lower()
            if any(keyword in lower for keyword in _VIDEO_KEYWORDS):
                return True
    for child in node.get("children", []):
        if _has_video_signals(child):
            return True
    return False


def analyze_tree(tree: dict) -> list:
    """
    Return presence tags for the AXWebArea subtree.

    Multiple tags can be present on a single screen; classification decides
    which one governs. This helper only reports structural signals.
    """
    web_area = _find_web_area(tree)
    counts = _count_roles(web_area)
    tags = []

    if counts.get("AXVideo", 0) > 0 or counts.get("AXMediaTimeline", 0) > 0 or _has_video_signals(web_area):
        tags.append("HAS_VIDEO")
    if counts.get("AXRadioButton", 0) > 0:
        tags.append("HAS_RADIO")
    if counts.get("AXCheckBox", 0) > 0:
        tags.append("HAS_CHECKBOX")
    if counts.get("AXTextArea", 0) + counts.get("AXTextField", 0) > 0:
        tags.append("HAS_TEXT_INPUT")
    if counts.get("AXComboBox", 0) + counts.get("AXPopUpButton", 0) > 0:
        tags.append("HAS_COMBOBOX")
    if counts.get("AXLink", 0) > 0:
        tags.append("HAS_LINKS")
    if counts.get("AXButton", 0) > 0:
        tags.append("HAS_BUTTONS")
    return tags
