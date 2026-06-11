"""
Capture macapptree format: accessibility tree + screenshot with bounding boxes.
This is for Spark Claude visual reference - element IDs + visible_bbox.
Screenshot scopes to the AX focused window (the same window capture_tree
walks) — not the on-screen frontmost window. So a stray Terminal or other
non-target app on top can't poison the LLM payload.
"""

import base64
import re
from io import BytesIO
from AppKit import NSWorkspace
from ApplicationServices import (
    AXUIElementCreateApplication,
    AXUIElementCopyAttributeValue,
    kAXErrorSuccess,
    kAXFocusedWindowAttribute,
    kAXPositionAttribute,
    kAXSizeAttribute,
)
from Quartz import (
    CGWindowListCopyWindowInfo,
    kCGWindowListOptionOnScreenOnly,
    kCGNullWindowID,
    CGWindowListCreateImage,
    CGRectNull,
    kCGWindowListOptionIncludingWindow,
    kCGWindowImageBoundsIgnoreFraming,
)
from AppKit import NSBitmapImageRep, NSPNGFileType

from app.tasks.capture_tree import capture_tree


def _ax_focused_window_bounds(pid: int):
    """Read AX focused window's [x, y, w, h] in points. None if not available."""
    app_elem = AXUIElementCreateApplication(pid)
    err, win = AXUIElementCopyAttributeValue(app_elem, kAXFocusedWindowAttribute, None)
    if err != kAXErrorSuccess or win is None:
        return None
    err, pos = AXUIElementCopyAttributeValue(win, kAXPositionAttribute, None)
    err2, size = AXUIElementCopyAttributeValue(win, kAXSizeAttribute, None)
    if err != kAXErrorSuccess or err2 != kAXErrorSuccess:
        return None
    pm = re.search(r"x:(-?[\d.]+)\s+y:(-?[\d.]+)", str(pos))
    sm = re.search(r"w:(-?[\d.]+)\s+h:(-?[\d.]+)", str(size))
    if not pm or not sm:
        return None
    return (float(pm.group(1)), float(pm.group(2)),
            float(sm.group(1)), float(sm.group(2)))


def capture_screenshot(app_name: str) -> bytes:
    """
    Capture the AX focused window of the application as PNG bytes.

    Defect 2026-06-11 15:15: the old "largest visible Chrome window" rule
    could pick the wrong Chrome window when the app has multiple
    similarly-sized windows, sending the LLM a screenshot of a sign-in
    window instead of the Khan exercise. Worse, when a non-target app was
    frontmost (Terminal, Finder), the captured window could be obscured.
    Solution: scope to the same window capture_tree reads (kAXFocusedWindow)
    by matching CGWindow bounds to the AX focused window's bounds. Fall
    back to largest-by-area only if no bounds match found.

    Args:
        app_name: Name of application (e.g., "Google Chrome")

    Returns:
        PNG image as bytes

    Raises:
        RuntimeError: If app not found or screenshot fails
    """
    workspace = NSWorkspace.sharedWorkspace()
    target_app = None
    for app in workspace.runningApplications():
        if app_name.lower() in app.localizedName().lower():
            target_app = app
            break

    if not target_app:
        raise RuntimeError(f"Application '{app_name}' not found")

    pid = target_app.processIdentifier()
    focused_bounds = _ax_focused_window_bounds(int(pid))

    window_list = CGWindowListCopyWindowInfo(
        kCGWindowListOptionOnScreenOnly, kCGNullWindowID,
    )
    pid_windows = [w for w in window_list if w.get("kCGWindowOwnerPID") == pid]

    window_id = None
    # Strategy 1: match the AX focused window by bounds (±2pt tolerance
    # for rounding between AX points and CGWindow units).
    if focused_bounds is not None:
        fx, fy, fw, fh = focused_bounds
        for w in pid_windows:
            b = w.get("kCGWindowBounds", {})
            if (abs(b.get("X", 0) - fx) < 2 and abs(b.get("Y", 0) - fy) < 2
                    and abs(b.get("Width", 0) - fw) < 2
                    and abs(b.get("Height", 0) - fh) < 2):
                window_id = w.get("kCGWindowNumber")
                break

    # Strategy 2: largest-by-area fallback (legacy behavior).
    if window_id is None:
        best_area = 0
        for w in pid_windows:
            b = w.get("kCGWindowBounds", {})
            area = b.get("Width", 0) * b.get("Height", 0)
            if area > best_area:
                best_area = area
                window_id = w.get("kCGWindowNumber")

    if not window_id:
        raise RuntimeError(f"No visible window found for '{app_name}'")

    # Capture window image
    image = CGWindowListCreateImage(
        CGRectNull,
        kCGWindowListOptionIncludingWindow,
        window_id,
        kCGWindowImageBoundsIgnoreFraming
    )

    if not image:
        raise RuntimeError(f"Failed to capture screenshot for '{app_name}'")

    # Convert to PNG bytes
    bitmap = NSBitmapImageRep.alloc().initWithCGImage_(image)
    png_data = bitmap.representationUsingType_properties_(NSPNGFileType, None)

    return bytes(png_data)


def capture_macapptree(app_name: str) -> dict:
    """
    Capture macapptree format: tree with element IDs + screenshot.

    The tree includes:
    - element_id: Stable ID for visual reference (Spark Claude only)
    - visible_bbox: [x, y, width, height] for drawing boxes on screenshot
    - name, role: For actual execution (Mac uses these, NOT element_id)

    Args:
        app_name: Name of application (e.g., "Acellus")

    Returns:
        dict with:
            - tree: Full accessibility tree with element_id and visible_bbox
            - screenshot_b64: Base64-encoded PNG screenshot

    Raises:
        RuntimeError: If capture fails
    """
    # Capture tree with element IDs and visible_bbox
    tree = capture_tree(app_name)

    # Capture screenshot
    screenshot_bytes = capture_screenshot(app_name)
    screenshot_b64 = base64.b64encode(screenshot_bytes).decode("utf-8")

    return {
        "tree": tree,
        "screenshot_b64": screenshot_b64
    }


if __name__ == "__main__":
    result = capture_macapptree("Acellus")
    print(f"Tree captured with {len(str(result['tree']))} chars")
    print(f"Screenshot: {len(result['screenshot_b64'])} chars base64")
