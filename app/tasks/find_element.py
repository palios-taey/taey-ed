# STATUS: FROZEN - Proven in v7. Verified 2026-02-19. Do not modify.
"""
Find element in accessibility tree by text.
Returns element with raw AXUIElement reference for clicking.

Feb 2026: Added retry logic. Chrome's accessibility tree at depth 16+
may not be immediately stable after page load. One retry after 1s delay.
"""

import logging
import time

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
)
from CoreFoundation import CFArrayGetCount, CFArrayGetValueAtIndex

logger = logging.getLogger("taey-ed")


def find_element(app_name: str, target_text: str, role: str = None, match_mode: str = "exact"):
    """
    Find element containing target text in any text field.
    Retries once after 1s if not found (Chrome deep tree stability).

    Args:
        app_name: Application name (e.g., "Acellus")
        target_text: Text to find (e.g., "START")
        role: Optional AX role filter (e.g., "AXButton") - only return
              elements with this role. Critical for quiz answers where
              AXStaticText and AXButton share the same text.
        match_mode: "exact" (default) for == comparison,
                    "contains" for substring match.
                    Coursera uses long link names; contains is needed.

    Returns:
        Raw AXUIElement reference, or None if not found
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

    def text_matches(element, text: str) -> bool:
        """Check if text matches any text field (exact or contains)."""
        for attr in [kAXTitleAttribute, kAXDescriptionAttribute, kAXValueAttribute]:
            err, val = AXUIElementCopyAttributeValue(element, attr, None)
            if err == kAXErrorSuccess and val:
                val_str = str(val)
                if match_mode == "contains":
                    if text in val_str:
                        return True
                else:
                    if val_str == text:
                        return True
        return False

    def role_matches(element, required_role: str) -> bool:
        """Check if element has the required AX role."""
        err, val = AXUIElementCopyAttributeValue(element, kAXRoleAttribute, None)
        if err == kAXErrorSuccess and val:
            return str(val) == required_role
        return False

    def search(element):
        """Recursively search for element."""
        # Role-only search when target_text is empty
        if not target_text and role:
            if role_matches(element, role):
                return element
        elif text_matches(element, target_text):
            if role is None or role_matches(element, role):
                return element

        err, children = AXUIElementCopyAttributeValue(element, kAXChildrenAttribute, None)
        if err == kAXErrorSuccess and children:
            count = CFArrayGetCount(children)
            for i in range(count):
                child = CFArrayGetValueAtIndex(children, i)
                result = search(child)
                if result:
                    return result
        return None

    # First attempt
    result = search(root)
    if result:
        # Log what we found for diagnostic
        err_r, r_val = AXUIElementCopyAttributeValue(result, kAXRoleAttribute, None)
        err_t, t_val = AXUIElementCopyAttributeValue(result, kAXTitleAttribute, None)
        err_v, v_val = AXUIElementCopyAttributeValue(result, kAXValueAttribute, None)
        found_role = str(r_val) if err_r == kAXErrorSuccess and r_val else "?"
        found_title = str(t_val) if err_t == kAXErrorSuccess and t_val else ""
        found_value = str(v_val)[:60] if err_v == kAXErrorSuccess and v_val else ""
        logger.info(f"find_element: FOUND role={found_role} title='{found_title}' value='{found_value}' (target='{target_text}', req_role={role})")
        return result

    # Retry once after delay - Chrome deep tree may not be stable yet
    logger.info(f"find_element: '{target_text}' not found, retrying in 1s...")
    time.sleep(1.0)
    root = AXUIElementCreateApplication(target_app.processIdentifier())
    return search(root)


def _find_web_area_element(element):
    """
    Find the AXWebArea element in the accessibility tree.
    Scopes searches to web content only, excluding browser chrome
    (toolbar buttons, tabs, extensions, other tabs' content).
    Returns the AXWebArea AXUIElement, or None if not found.

    Uses a list accumulator instead of identity comparison (is/is not)
    because PyObjC bridge objects don't have stable Python identity.
    """
    found = []

    def _search(el):
        if found:
            return  # Already found one
        err_r, r_val = AXUIElementCopyAttributeValue(el, kAXRoleAttribute, None)
        if err_r == kAXErrorSuccess and r_val and str(r_val) == "AXWebArea":
            found.append(el)
            return

        err, children = AXUIElementCopyAttributeValue(el, kAXChildrenAttribute, None)
        if err == kAXErrorSuccess and children:
            count = CFArrayGetCount(children)
            for i in range(count):
                if found:
                    return
                child = CFArrayGetValueAtIndex(children, i)
                _search(child)

    _search(element)
    return found[0] if found else None


def find_all_elements(app_name: str, role: str, description_contains: str = None, match_mode: str = "contains"):
    """
    Find ALL elements matching role (and optional description text).
    Returns list of (AXUIElement, description_text) tuples.

    Scoped to AXWebArea subtree to avoid picking up browser chrome
    (toolbar buttons, other tabs' elements, extensions).

    Used for discovery of multiple popup buttons, checkboxes, etc.
    """
    workspace = NSWorkspace.sharedWorkspace()
    target_app = None
    for app in workspace.runningApplications():
        if app_name.lower() in app.localizedName().lower():
            target_app = app
            break

    if not target_app:
        raise RuntimeError(f"Application '{app_name}' not found")

    root = AXUIElementCreateApplication(target_app.processIdentifier())

    # Scope to AXWebArea to avoid searching entire Chrome tree
    web_area = _find_web_area_element(root)
    if web_area is not None:
        logger.info("find_all_elements: scoped to AXWebArea subtree")
    else:
        logger.warning("find_all_elements: AXWebArea not found, searching full tree")
        web_area = root  # Fall back to full tree

    results = []

    def search(element):
        err_r, r_val = AXUIElementCopyAttributeValue(element, kAXRoleAttribute, None)
        if err_r == kAXErrorSuccess and r_val and str(r_val) == role:
            # Get description/title/value for this element
            desc = ""
            for attr in [kAXDescriptionAttribute, kAXTitleAttribute, kAXValueAttribute]:
                err, val = AXUIElementCopyAttributeValue(element, attr, None)
                if err == kAXErrorSuccess and val:
                    desc = str(val)
                    break

            if description_contains:
                if description_contains.lower() in desc.lower():
                    results.append((element, desc))
            else:
                results.append((element, desc))

        err, children = AXUIElementCopyAttributeValue(element, kAXChildrenAttribute, None)
        if err == kAXErrorSuccess and children:
            count = CFArrayGetCount(children)
            for i in range(count):
                child = CFArrayGetValueAtIndex(children, i)
                search(child)

    search(web_area)

    # Fallback: if web-area search found nothing and we were scoped,
    # search full app tree. Catches React Portal elements (combobox popups,
    # dropdown menus) that render OUTSIDE AXWebArea at shallow depth.
    if not results and web_area is not root:
        logger.info("find_all_elements: 0 results in AXWebArea, searching full app tree (React Portal fallback)")
        search(root)

    logger.info(f"find_all_elements: Found {len(results)} elements with role={role}" +
                (f" containing '{description_contains}'" if description_contains else ""))
    return results


if __name__ == "__main__":
    element = find_element("Acellus", "START")
    if element:
        print(f"Found: {element}")
    else:
        print("Not found")
