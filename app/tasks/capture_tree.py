"""
Capture accessibility tree from a Mac application.
Single function. No filtering. Returns full tree as dict.
Includes element_id and visible_bbox for macapptree format.
"""

from AppKit import NSWorkspace
from ApplicationServices import (
    AXUIElementCreateApplication,
    AXUIElementCopyAttributeValue,
    kAXErrorSuccess,
    kAXChildrenAttribute,
    kAXRoleAttribute,
    kAXTitleAttribute,
    kAXDescriptionAttribute,
    kAXValueAttribute,
    kAXPositionAttribute,
    kAXSizeAttribute,
    kAXMainAttribute,
    kAXFocusedAttribute,
)
from CoreFoundation import CFArrayGetCount, CFArrayGetValueAtIndex
import re
import hashlib


def capture_tree(app_name: str) -> dict:
    """
    Capture full accessibility tree from application.

    Args:
        app_name: Name of application (e.g., "Acellus")

    Returns:
        Dict with full tree structure

    Raises:
        RuntimeError: If app not found
    """
    # Find app
    workspace = NSWorkspace.sharedWorkspace()
    target_app = None
    for app in workspace.runningApplications():
        if app_name.lower() in app.localizedName().lower():
            target_app = app
            break

    if not target_app:
        raise RuntimeError(f"Application '{app_name}' not found")

    root = AXUIElementCreateApplication(target_app.processIdentifier())

    def get_node(element, path: str = "root") -> dict:
        """Extract info from one element with element_id for visual reference."""
        node = {}

        # Role
        err, val = AXUIElementCopyAttributeValue(element, kAXRoleAttribute, None)
        if err == kAXErrorSuccess and val:
            node["role"] = str(val)

        # Title
        err, val = AXUIElementCopyAttributeValue(element, kAXTitleAttribute, None)
        if err == kAXErrorSuccess and val:
            node["title"] = str(val)

        # Description
        err, val = AXUIElementCopyAttributeValue(element, kAXDescriptionAttribute, None)
        if err == kAXErrorSuccess and val:
            node["description"] = str(val)

        # Value
        err, val = AXUIElementCopyAttributeValue(element, kAXValueAttribute, None)
        if err == kAXErrorSuccess and val:
            node["value"] = str(val)

        # Name = title or description (for matching convenience)
        node["name"] = node.get("title") or node.get("description") or ""

        # Position
        pos_x, pos_y = None, None
        err, val = AXUIElementCopyAttributeValue(element, kAXPositionAttribute, None)
        if err == kAXErrorSuccess and val:
            m = re.search(r"x:([\d.]+)\s+y:([\d.]+)", str(val))
            if m:
                pos_x, pos_y = int(float(m.group(1))), int(float(m.group(2)))
                node["position"] = [pos_x, pos_y]

        # Size
        width, height = None, None
        err, val = AXUIElementCopyAttributeValue(element, kAXSizeAttribute, None)
        if err == kAXErrorSuccess and val:
            m = re.search(r"w:([\d.]+)\s+h:([\d.]+)", str(val))
            if m:
                width, height = int(float(m.group(1))), int(float(m.group(2)))
                node["size"] = [width, height]

        # visible_bbox: [x, y, width, height] for Spark Claude visual reference
        if pos_x is not None and width is not None:
            node["visible_bbox"] = [pos_x, pos_y, width, height]

        # Window/element focus state (Jesse 2026-06-01: capture both so the
        # server can disambiguate the active AXWebArea when a browser process
        # has multiple windows open — e.g. Khan + a sign-in tab in a
        # background window). Captured on EVERY element where the AX call
        # succeeds — no proactive filtering. Server picks what it wants.
        err, val = AXUIElementCopyAttributeValue(element, kAXMainAttribute, None)
        if err == kAXErrorSuccess and val is not None:
            node["main"] = bool(val)
        err, val = AXUIElementCopyAttributeValue(element, kAXFocusedAttribute, None)
        if err == kAXErrorSuccess and val is not None:
            node["focused"] = bool(val)

        # Generate stable element_id from path + role + name (for Spark Claude visual reference ONLY)
        # NOTE: This ID is for visual mapping only - Mac executes by name+role, NOT element_id
        id_source = f"{path}:{node.get('role', '')}:{node.get('name', '')}"
        node["element_id"] = hashlib.md5(id_source.encode()).hexdigest()[:16]

        # Children
        err, children = AXUIElementCopyAttributeValue(element, kAXChildrenAttribute, None)
        if err == kAXErrorSuccess and children:
            count = CFArrayGetCount(children)
            if count > 0:
                node["children"] = []
                for i in range(count):
                    child = CFArrayGetValueAtIndex(children, i)
                    child_path = f"{path}.{i}"
                    node["children"].append(get_node(child, child_path))

        return node

    return get_node(root)


if __name__ == "__main__":
    import json
    tree = capture_tree("Acellus")
    print(json.dumps(tree, indent=2))
