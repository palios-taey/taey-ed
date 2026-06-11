"""
Capture accessibility tree from a Mac application.
Single function. No filtering. Returns full tree as dict.
Includes element_id and visible_bbox for macapptree format.
"""

from AppKit import NSWorkspace
from ApplicationServices import (
    AXUIElementCreateApplication,
    AXUIElementCopyAttributeValue,
    AXUIElementSetAttributeValue,
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
    kAXFocusedWindowAttribute,
)
from CoreFoundation import CFArrayGetCount, CFArrayGetValueAtIndex
import hashlib
import re
import time

# Per-process cache of PIDs where AXManualAccessibility has been set, so the
# capture_tree call doesn't redundantly set the attribute on every poll.
_ax_complete_pids: set[int] = set()

# How long to wait after first-time AXManualAccessibility set, to let Chrome
# rebuild its accessibility tree into complete mode. Without this, the very
# first capture after the setter races the rebuild and gets a half-built
# tree (taey-ed field report 2026-06-11 11:12: AXTable/AXRow/AXCell
# structure present but AXStaticText names empty and heights 0). One-shot
# cost — subsequent captures hit the PID cache and skip this delay.
_AX_REBUILD_WAIT_S = 0.8


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

    pid = int(target_app.processIdentifier())
    app_elem = AXUIElementCreateApplication(pid)

    # Force Chrome (and other Chromium browsers) into complete-AX mode.
    # Per taey-ed dispatch 2026-06-11 (Family-verified vs Perseus/Chromium):
    # Chrome only builds the full accessibility tree when assistive tech is
    # detected or accessibility is explicitly enabled. Without this, widget
    # extents (w/h), value-bank items, and full nested cells in Perseus
    # matchers come through as empty AXCells with zero geometry. Setting
    # AXManualAccessibility=True on the Chrome app element switches it to
    # complete mode. Surgical: no window-repositioning side effect (unlike
    # AXEnhancedUserInterface=True). Idempotent — Chromium handles repeat
    # sets fine. Cached per PID so we don't re-set on every poll tick.
    if pid not in _ax_complete_pids:
        name_lower = (target_app.localizedName() or "").lower()
        if any(b in name_lower for b in ("chrome", "chromium", "edge", "brave")):
            AXUIElementSetAttributeValue(
                app_elem, "AXManualAccessibility", True,
            )
            # Let Chrome finish rebuilding its accessibility tree into
            # complete mode before we read AXFocusedWindow or walk
            # children. Without this wait the FIRST capture-tree call
            # after a fresh Chrome PID returns half-built nodes (taey-ed
            # field report 2026-06-11 11:12). One-shot per PID — the
            # _ax_complete_pids cache skips this branch on every
            # subsequent call.
            time.sleep(_AX_REBUILD_WAIT_S)
        _ax_complete_pids.add(pid)

    # Scope to the FOCUSED WINDOW only (Jesse 2026-06-01 root-fix).
    # Latent bug since the file was added 2026-02-20 (commit 0837e83):
    # the capture used to root at the whole-app element and walk every
    # window in the process. With one Chrome window open the resulting
    # tree had a single AXWebArea so downstream queries appeared to
    # work; with TWO windows (Khan + a Sign-in tab) there are two
    # AXWebAreas and queries pick whichever was enumerated first —
    # often the wrong one. kAXFocusedWindow returns the app's OWN
    # frontmost window regardless of macOS-frontmost (so it works
    # even when Chrome is backgrounded by Screen Sharing or another
    # app), producing exactly one AXWebArea per capture.
    err, focused_win = AXUIElementCopyAttributeValue(
        app_elem, kAXFocusedWindowAttribute, None,
    )
    if err == kAXErrorSuccess and focused_win is not None:
        root = focused_win
    else:
        # No focused window — degraded fallback. Capture the whole-app
        # element so downstream consumers still get *something* rather
        # than a hard failure. This is the pre-fix shape; happens when
        # the target app has no windows open at all.
        root = app_elem

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

        # AXEnabled — captured on every element where it's available. Critical
        # for interaction validation: a Check button's gray->active transition
        # is a first-class completion indicator that has NO bbox change, so
        # server-side validators that bbox-compare are blind to it. Taey-ed
        # field report 2026-06-11 13:00 + manual ranking-widget drag run
        # confirmed the gap. Captured uniformly (no role filter) — server
        # picks what it wants; same shape as main/focused above.
        err, val = AXUIElementCopyAttributeValue(element, "AXEnabled", None)
        if err == kAXErrorSuccess and val is not None:
            node["enabled"] = bool(val)

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
