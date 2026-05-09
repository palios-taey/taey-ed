"""
Click element using accessibility actions or mouse/keyboard simulation.

Five strategies:
- "ax_press" (default): AXUIElementPerformAction with kAXPressAction.
  Works for native Mac apps (Acellus, etc.)
- "focus_enter": Set focus, simulate Enter keypress.
  For browser buttons/links.
- "focus_space": Set focus, simulate Space keypress.
  For standard radio buttons/checkboxes.
- "focus_press": Set AX focus on the element first (drives real DOM focus),
  then AXPress. The VoiceOver path. For ARIA listbox options where the
  keyboard handler lives on a wrapper element and options are React-portaled
  outside it (e.g. Khan Wonder Blocks SingleSelect / DropdownCore).
- "mouse_click": Move mouse to element center, click.
  Most reliable for browser elements. Works for custom React components
  (Coursera radio buttons, etc.) where keyboard events don't fire handlers.

Discovery history:
  Jan 2026 - AXPress returns success on Chrome but JS doesn't respond.
  Feb 2026 - AXPosition returns AXValueRef, not NSPoint. Must str() + regex parse.
  May 2026 - Wonder Blocks listbox needed focus_press (kAXFocused → AXPress).
"""

import logging
import re
import time

from AppKit import NSRunningApplication, NSApplicationActivateIgnoringOtherApps
from ApplicationServices import (
    AXUIElementPerformAction,
    AXUIElementSetAttributeValue,
    AXUIElementCopyAttributeValue,
    AXUIElementGetPid,
    kAXErrorSuccess,
    kAXPressAction,
    kAXFocusedAttribute,
)

from Quartz import (
    CGEventCreateKeyboardEvent,
    CGEventCreateMouseEvent,
    CGEventPost,
    kCGHIDEventTap,
    kCGEventLeftMouseDown,
    kCGEventLeftMouseUp,
    kCGMouseButtonLeft,
)

logger = logging.getLogger("taey-ed")


def click_element(element, strategy: str = "ax_press") -> bool:
    """
    Click element via accessibility action.

    Args:
        element: Raw AXUIElement reference
        strategy: "ax_press" for native apps,
                  "focus_enter" for browser buttons/links,
                  "focus_space" for browser radio/checkbox,
                  "mouse_click" for browser custom components (most reliable)

    Returns:
        True on success

    Raises:
        RuntimeError on failure
    """
    if element is None:
        raise RuntimeError("Cannot click None element")

    if strategy == "mouse_click":
        return _mouse_click(element)
    elif strategy == "focus_space":
        return _focus_and_key(element, keycode=49)  # Space
    elif strategy == "focus_enter":
        return _focus_and_key(element, keycode=36)  # Enter/Return
    elif strategy in ("focus_press", "vo_press"):
        return _focus_then_press(element)
    else:
        return _ax_press(element)


def _ax_press(element) -> bool:
    """Click via AXPress action (native apps)."""
    err = AXUIElementPerformAction(element, kAXPressAction)
    if err != kAXErrorSuccess:
        raise RuntimeError(f"AXPress failed with error code: {err}")
    return True


def _focus_then_press(element) -> bool:
    """VoiceOver path: AX focus first, then AXPress.

    For ARIA listbox options (Wonder Blocks SingleSelect, React Aria
    combobox listitems). Setting kAXFocused on the AXMenuItem drives real
    DOM focus onto the underlying portaled <div>; the subsequent AXPress
    triggers element.click() on the now-focused element, which propagates
    through React's normal event chain.

    Synthetic mouse-clicks and synthetic Down/Enter to the document fail
    on these widgets because the keyboard handler lives on a wrapper that
    is not the active descendant — see WONDER_FIX.md research note.
    """
    _activate_element_app(element)

    err = AXUIElementSetAttributeValue(element, kAXFocusedAttribute, True)
    if err != kAXErrorSuccess:
        raise RuntimeError(f"focus_press: setting AX focus failed (err={err})")

    # Allow Chrome's accessibility shim to drive DOM focus + React re-render.
    time.sleep(0.25)

    err = AXUIElementPerformAction(element, kAXPressAction)
    if err != kAXErrorSuccess:
        raise RuntimeError(f"focus_press: AXPress after focus failed (err={err})")

    return True


def _activate_element_app(element):
    """Bring the element's owning app to foreground.

    Gets the PID from the AXUIElement and activates that process.
    Works for any app (Chrome, Acellus, etc.) - not hardcoded to Chrome.
    """
    err, pid = AXUIElementGetPid(element, None)
    if err != kAXErrorSuccess:
        logger.warning(f"Cannot get PID from element (error {err}), skipping activation")
        return False

    app = NSRunningApplication.runningApplicationWithProcessIdentifier_(pid)
    if app:
        app.activateWithOptions_(NSApplicationActivateIgnoringOtherApps)
        time.sleep(0.3)
        logger.debug(f"Activated app: {app.localizedName()} (pid={pid})")
        return True

    logger.warning(f"No running app found for pid={pid}")
    return False


def _parse_ax_coords(pos_ref, size_ref):
    """Parse AXValueRef position/size into (x, y, w, h) floats."""
    pos_str = str(pos_ref)
    size_str = str(size_ref)
    pos_match = re.search(r"x:(-?[\d.]+)\s+y:(-?[\d.]+)", pos_str)
    size_match = re.search(r"w:(-?[\d.]+)\s+h:(-?[\d.]+)", size_str)
    if not pos_match or not size_match:
        raise RuntimeError(
            f"Cannot parse AXPosition/AXSize: pos={pos_str}, size={size_str}"
        )
    return (
        float(pos_match.group(1)),
        float(pos_match.group(2)),
        float(size_match.group(1)),
        float(size_match.group(2)),
    )


def _get_element_rect(element):
    """Get element (x, y, w, h) from AXPosition/AXSize."""
    err, pos = AXUIElementCopyAttributeValue(element, "AXPosition", None)
    if err != kAXErrorSuccess:
        raise RuntimeError(f"Cannot get element position (error {err})")
    err, size = AXUIElementCopyAttributeValue(element, "AXSize", None)
    if err != kAXErrorSuccess:
        raise RuntimeError(f"Cannot get element size (error {err})")
    return _parse_ax_coords(pos, size)


class StaleElementError(RuntimeError):
    """Element has invalid coordinates - likely behind overlay or stale after transition."""
    pass


# Reasonable screen bounds (max retina resolution)
MAX_SCREEN_X = 7680
MAX_SCREEN_Y = 4320


def _mouse_click(element) -> bool:
    """
    Click by moving mouse to element center and clicking.

    Most reliable strategy for browser elements. Works for:
    - Custom React radio buttons (Coursera)
    - Custom checkboxes, toggle switches
    - Any element with JS click handlers

    Gets element position via AXPosition/AXSize, calculates center,
    posts CGEvent mouse down+up at that point.

    Feb 2026: Chrome reports off-screen elements with h=0 and y=viewport_bottom.
    When h=0 detected, sets focus to scroll element into view, then re-reads position.

    Feb 2026: Validates coordinates are positive and within screen bounds.
    Negative coords (e.g. x:-1105) indicate stale/overlay-hidden elements.
    Raises StaleElementError so callers can re-match the screen.
    """
    _activate_element_app(element)

    x, y, w, h = _get_element_rect(element)
    logger.info(f"Initial rect: pos=({x:.0f},{y:.0f}) size=({w:.0f},{h:.0f})")

    # Coordinate sanity check: negative or absurdly large = stale/overlay element
    if x < 0 or y < 0 or x > MAX_SCREEN_X or y > MAX_SCREEN_Y:
        raise StaleElementError(
            f"Element coordinates out of bounds: ({x:.0f},{y:.0f}). "
            f"Likely stale or behind overlay. Re-match screen."
        )
    if w < 0 or h < 0:
        raise StaleElementError(
            f"Element has negative dimensions: w={w:.0f} h={h:.0f}. "
            f"Likely stale or behind overlay. Re-match screen."
        )

    # Off-screen detection: Chrome reports h=0 for elements below the fold
    if h == 0:
        logger.info("Element has h=0 (off-screen) — setting focus to scroll into view...")
        err = AXUIElementSetAttributeValue(element, kAXFocusedAttribute, True)
        if err != kAXErrorSuccess:
            logger.warning(f"Focus-to-scroll failed (error {err}), trying click anyway")
        else:
            # Wait for Chrome to scroll and repaint
            time.sleep(0.5)
            # Re-read position after scroll
            x, y, w, h = _get_element_rect(element)
            logger.info(f"After scroll rect: pos=({x:.0f},{y:.0f}) size=({w:.0f},{h:.0f})")
            if h == 0:
                logger.warning("Element still h=0 after focus scroll — clicking at reported position anyway")

    # Final bounds check after any scroll adjustment
    if x < 0 or y < 0 or x > MAX_SCREEN_X or y > MAX_SCREEN_Y:
        raise StaleElementError(
            f"Element coordinates out of bounds after scroll: ({x:.0f},{y:.0f}). "
            f"Likely stale or behind overlay. Re-match screen."
        )

    # Calculate center (use h/2 even if h>0 for normal elements)
    cx = x + w / 2.0
    cy = y + h / 2.0 if h > 0 else y
    center = (cx, cy)

    logger.info(f"Mouse click at ({cx:.0f}, {cy:.0f})")

    # Mouse down
    mouse_down = CGEventCreateMouseEvent(
        None, kCGEventLeftMouseDown, center, kCGMouseButtonLeft
    )
    CGEventPost(kCGHIDEventTap, mouse_down)
    time.sleep(0.1)

    # Mouse up
    mouse_up = CGEventCreateMouseEvent(
        None, kCGEventLeftMouseUp, center, kCGMouseButtonLeft
    )
    CGEventPost(kCGHIDEventTap, mouse_up)

    return True


def _focus_and_key(element, keycode: int = 36) -> bool:
    """
    Click via Focus + key simulation (browsers).

    Args:
        element: AXUIElement to click
        keycode: macOS keycode. 36=Enter/Return, 49=Space.
    """
    _activate_element_app(element)

    # Focus the element
    err = AXUIElementSetAttributeValue(element, kAXFocusedAttribute, True)
    if err != kAXErrorSuccess:
        raise RuntimeError(f"Setting focus failed with error code: {err}")

    time.sleep(0.3)

    # Simulate keypress
    event_down = CGEventCreateKeyboardEvent(None, keycode, True)
    CGEventPost(kCGHIDEventTap, event_down)
    time.sleep(0.05)
    event_up = CGEventCreateKeyboardEvent(None, keycode, False)
    CGEventPost(kCGHIDEventTap, event_up)

    return True


if __name__ == "__main__":
    from find_element import find_element

    element = find_element("Acellus", "START")
    if element:
        click_element(element)
        print("Clicked!")
    else:
        print("Element not found")
