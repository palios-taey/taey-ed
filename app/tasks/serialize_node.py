"""
serialize_ax_node — extract the raw AX attribute dict from a single AXUIElement.

Per Jesse 2026-05-19 architectural principle ("Mac is dumb capture/execute,
server is smart"): handlers that interact with AX elements must surface the
FULL raw node data to downstream consumers, not a Mac-filtered subset.

This function is the canonical single-element analog of capture_tree.get_node —
same attribute coverage, no recursion into children.
"""

import re
from ApplicationServices import (
    AXUIElementCopyAttributeValue,
    kAXErrorSuccess,
    kAXRoleAttribute,
    kAXTitleAttribute,
    kAXDescriptionAttribute,
    kAXValueAttribute,
    kAXPositionAttribute,
    kAXSizeAttribute,
    kAXMainAttribute,
    kAXFocusedAttribute,
)


def serialize_ax_node(element) -> dict:
    """Pull the standard AX attributes off one element. No children.

    Returns a dict with keys: role, title, description, value, name (synth
    of title-or-description), position [x, y], size [w, h], visible_bbox
    [x, y, w, h]. Missing attributes are omitted from the dict.
    """
    if element is None:
        return {}
    node: dict = {}

    err, val = AXUIElementCopyAttributeValue(element, kAXRoleAttribute, None)
    if err == kAXErrorSuccess and val:
        node["role"] = str(val)

    err, val = AXUIElementCopyAttributeValue(element, kAXTitleAttribute, None)
    if err == kAXErrorSuccess and val:
        node["title"] = str(val)

    err, val = AXUIElementCopyAttributeValue(element, kAXDescriptionAttribute, None)
    if err == kAXErrorSuccess and val:
        node["description"] = str(val)

    err, val = AXUIElementCopyAttributeValue(element, kAXValueAttribute, None)
    if err == kAXErrorSuccess and val:
        node["value"] = str(val)

    # name = title-or-description (the same synth capture_tree.get_node uses).
    node["name"] = node.get("title") or node.get("description") or ""

    pos_x = pos_y = None
    err, val = AXUIElementCopyAttributeValue(element, kAXPositionAttribute, None)
    if err == kAXErrorSuccess and val:
        m = re.search(r"x:([\d.]+)\s+y:([\d.]+)", str(val))
        if m:
            pos_x, pos_y = int(float(m.group(1))), int(float(m.group(2)))
            node["position"] = [pos_x, pos_y]

    width = height = None
    err, val = AXUIElementCopyAttributeValue(element, kAXSizeAttribute, None)
    if err == kAXErrorSuccess and val:
        m = re.search(r"w:([\d.]+)\s+h:([\d.]+)", str(val))
        if m:
            width, height = int(float(m.group(1))), int(float(m.group(2)))
            node["size"] = [width, height]

    if pos_x is not None and width is not None:
        node["visible_bbox"] = [pos_x, pos_y, width, height]

    # Window/element focus state (Jesse 2026-06-01: mirror what capture_tree
    # captures so handler-echo AX dicts carry the same disambiguation signal).
    err, val = AXUIElementCopyAttributeValue(element, kAXMainAttribute, None)
    if err == kAXErrorSuccess and val is not None:
        node["main"] = bool(val)
    err, val = AXUIElementCopyAttributeValue(element, kAXFocusedAttribute, None)
    if err == kAXErrorSuccess and val is not None:
        node["focused"] = bool(val)

    return node
