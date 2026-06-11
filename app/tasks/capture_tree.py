"""
Capture accessibility tree from a Mac application — FULL CAPTURE.

Every node, every available AX attribute. No allowlist. No filtering on the
Mac. All disambiguation, projection, and reduction happens server-side.

Per Jesse 2026-06-11: this is a day-1 architectural requirement; field
additions one at a time are explicitly the wrong shape. If transport size
becomes a problem we change transport, never the content.
"""

from AppKit import NSWorkspace
from ApplicationServices import (
    AXUIElementCreateApplication,
    AXUIElementCopyAttributeValue,
    AXUIElementCopyAttributeNames,
    AXUIElementSetAttributeValue,
    AXUIElementGetTypeID,
    kAXErrorSuccess,
    kAXChildrenAttribute,
    kAXFocusedWindowAttribute,
)
from CoreFoundation import (
    CFArrayGetCount, CFArrayGetValueAtIndex, CFArrayGetTypeID, CFGetTypeID,
)
import hashlib
import re
import time

# Per-process cache of PIDs where AXManualAccessibility has been set, so the
# capture_tree call doesn't redundantly set the attribute on every poll.
_ax_complete_pids: set[int] = set()

# How long to wait after first-time AXManualAccessibility set, to let Chrome
# rebuild its accessibility tree into complete mode. Without this, the very
# first capture after the setter races the rebuild and gets a half-built
# tree (taey-ed field report 2026-06-11 11:12). One-shot cost — subsequent
# captures hit the PID cache and skip this delay.
_AX_REBUILD_WAIT_S = 0.8

# CFTypeIDs used to dispatch attribute values without per-attribute coding.
_AX_ELEMENT_TYPE_ID = AXUIElementGetTypeID()
_CF_ARRAY_TYPE_ID = CFArrayGetTypeID()


def _to_jsonable(val):
    """Best-effort conversion of an AX value to a JSON-friendly form.

    Scalars (bool/int/float/str) pass through. Bytes decode UTF-8. Everything
    else (AXValue position/size/range structs, NSURLs, NSDates, etc.) falls
    back to its str() representation. Server can re-parse if it wants.
    """
    if val is None:
        return None
    if isinstance(val, bool):
        return val
    if isinstance(val, (int, float)):
        return val
    if isinstance(val, str):
        return val
    if isinstance(val, bytes):
        try:
            return val.decode("utf-8", errors="replace")
        except Exception:
            return repr(val)
    return str(val)


def capture_tree(app_name: str) -> dict:
    """
    Capture full accessibility tree from application.

    Args:
        app_name: Name of application (e.g., "Acellus", "Google Chrome")

    Returns:
        Dict with full tree structure — every node carries every readable
        AX attribute the system exposes for it.

    Raises:
        RuntimeError: If app not found
    """
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
    # Chrome only builds the full accessibility tree when assistive tech is
    # detected or accessibility is explicitly enabled. Without this, widget
    # extents, value-bank items, and full nested cells in Perseus matchers
    # come through as empty AXCells with zero geometry. Idempotent — repeat
    # sets are fine. Cached per PID. Renderer PID changes (Cmd+R reload,
    # tab close+reopen) get a fresh entry naturally when their pid isn't in
    # the cache.
    if pid not in _ax_complete_pids:
        name_lower = (target_app.localizedName() or "").lower()
        if any(b in name_lower for b in ("chrome", "chromium", "edge", "brave")):
            AXUIElementSetAttributeValue(
                app_elem, "AXManualAccessibility", True,
            )
            # Let Chrome finish rebuilding its accessibility tree into
            # complete mode before we read AXFocusedWindow or walk children.
            time.sleep(_AX_REBUILD_WAIT_S)
        _ax_complete_pids.add(pid)

    # Scope to the FOCUSED WINDOW only. kAXFocusedWindow returns the app's
    # own frontmost window regardless of macOS-frontmost, producing exactly
    # one AXWebArea per capture even when Chrome has multiple windows open.
    err, focused_win = AXUIElementCopyAttributeValue(
        app_elem, kAXFocusedWindowAttribute, None,
    )
    if err == kAXErrorSuccess and focused_win is not None:
        root = focused_win
    else:
        root = app_elem

    def get_node(element, path: str = "root") -> dict:
        """Serialize one element with EVERY readable attribute, then recurse."""
        node: dict = {}
        raw_pos = None
        raw_size = None

        err, names = AXUIElementCopyAttributeNames(element, None)
        if err != kAXErrorSuccess or not names:
            # Element refused to enumerate; still emit element_id so the
            # caller knows it existed. Path-stable id.
            node["element_id"] = hashlib.md5(
                f"{path}::".encode()
            ).hexdigest()[:16]
            return node

        for name in names:
            name_str = str(name)
            err, val = AXUIElementCopyAttributeValue(element, name_str, None)
            if err != kAXErrorSuccess or val is None:
                continue

            # Lowercase-camel JSON key with the AX prefix stripped. Server
            # convention (existing fields role/title/description/value/...).
            if name_str.startswith("AX") and len(name_str) > 2:
                json_key = name_str[2].lower() + name_str[3:]
            else:
                json_key = name_str

            try:
                tid = CFGetTypeID(val)
            except Exception:
                tid = None

            # Single AXUIElement reference (AXParent, AXTopLevelUIElement,
            # AXWindow, AXFocusedUIElement, etc.) — skip to avoid duplicating
            # the tree on every node. The Children walk covers structural
            # traversal; per-node state lives in scalar attrs.
            if tid == _AX_ELEMENT_TYPE_ID:
                continue

            if tid == _CF_ARRAY_TYPE_ID:
                count = CFArrayGetCount(val)
                # AXChildren — the one array we recurse on.
                if name_str == kAXChildrenAttribute or name_str == "AXChildren":
                    children = []
                    for i in range(count):
                        child = CFArrayGetValueAtIndex(val, i)
                        children.append(get_node(child, f"{path}.{i}"))
                    node["children"] = children
                    continue
                # Array of elements (AXSelectedChildren, AXVisibleChildren,
                # AXRows, AXColumns, AXLinkedUIElements, etc.) — skip to avoid
                # duplicating the tree. Server can reconstruct selection /
                # visibility from per-node AXSelected/AXVisible flags.
                if count > 0:
                    first = CFArrayGetValueAtIndex(val, 0)
                    first_tid = None
                    try:
                        first_tid = CFGetTypeID(first)
                    except Exception:
                        pass
                    if first_tid == _AX_ELEMENT_TYPE_ID:
                        continue
                # Array of scalars — serialize each.
                items = []
                for i in range(count):
                    items.append(_to_jsonable(CFArrayGetValueAtIndex(val, i)))
                node[json_key] = items
                continue

            # Scalar / AXValue / anything else — best-effort serialize.
            serialized = _to_jsonable(val)
            node[json_key] = serialized

            # Capture raw position/size strings for geometry derivation
            # (str(val) for AXValue gives "x:NNN y:NNN" / "w:NNN h:NNN").
            if name_str == "AXPosition":
                raw_pos = serialized
            elif name_str == "AXSize":
                raw_size = serialized

        # --- Additive derived fields (computed, not filtered) ---
        # Convenience: name = title or description, used by find_and_click.
        node["name"] = node.get("title") or node.get("description") or ""

        # Geometry derivation: [x, y, w, h] in points, same space as click_at.
        pos_x = pos_y = width = height = None
        if isinstance(raw_pos, str):
            m = re.search(r"x:(-?[\d.]+)\s+y:(-?[\d.]+)", raw_pos)
            if m:
                pos_x = int(float(m.group(1)))
                pos_y = int(float(m.group(2)))
                node["position"] = [pos_x, pos_y]
        if isinstance(raw_size, str):
            m = re.search(r"w:(-?[\d.]+)\s+h:(-?[\d.]+)", raw_size)
            if m:
                width = int(float(m.group(1)))
                height = int(float(m.group(2)))
                node["size"] = [width, height]
        if pos_x is not None and width is not None:
            node["visible_bbox"] = [pos_x, pos_y, width, height]

        # Stable element_id from path + role + name (visual reference only;
        # Mac executes by name+role, not element_id).
        id_source = f"{path}:{node.get('role', '')}:{node.get('name', '')}"
        node["element_id"] = hashlib.md5(id_source.encode()).hexdigest()[:16]

        return node

    return get_node(root)


if __name__ == "__main__":
    import json
    tree = capture_tree("Acellus")
    print(json.dumps(tree, indent=2))
