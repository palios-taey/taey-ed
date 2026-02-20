# STATUS: FROZEN - Proven in v7. Verified 2026-02-19. Do not modify.
"""
Capture macapptree format: accessibility tree + screenshot with bounding boxes.
This is for Spark Claude visual reference - element IDs + visible_bbox.
Single function. FREEZE once working.
"""

import base64
from io import BytesIO
from AppKit import NSWorkspace
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


def capture_screenshot(app_name: str) -> bytes:
    """
    Capture screenshot of application window as PNG bytes.

    Args:
        app_name: Name of application (e.g., "Acellus")

    Returns:
        PNG image as bytes

    Raises:
        RuntimeError: If app not found or screenshot fails
    """
    # Find app's window ID
    workspace = NSWorkspace.sharedWorkspace()
    target_app = None
    for app in workspace.runningApplications():
        if app_name.lower() in app.localizedName().lower():
            target_app = app
            break

    if not target_app:
        raise RuntimeError(f"Application '{app_name}' not found")

    pid = target_app.processIdentifier()

    # Find the LARGEST window belonging to this app.
    # Apps like Firefox have multiple windows (main browser + tooltips/popups).
    # CGWindowListCopyWindowInfo returns z-order, so the first match might be
    # a tiny tooltip. Pick the largest by pixel area to get the main window.
    window_list = CGWindowListCopyWindowInfo(kCGWindowListOptionOnScreenOnly, kCGNullWindowID)
    window_id = None
    best_area = 0
    for window in window_list:
        if window.get("kCGWindowOwnerPID") == pid:
            bounds = window.get("kCGWindowBounds", {})
            area = bounds.get("Width", 0) * bounds.get("Height", 0)
            if area > best_area:
                best_area = area
                window_id = window.get("kCGWindowNumber")

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
