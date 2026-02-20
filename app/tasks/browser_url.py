# STATUS: FROZEN - Proven in v7. Verified 2026-02-19. Do not modify.
"""
Browser URL verification via macOS accessibility API.

V1: Simple domain check using Chrome/Safari address bar AXTextField.
Uses the same AXUIElement APIs as capture_tree.py — no AppleScript needed.
"""

import logging
from AppKit import NSWorkspace
from ApplicationServices import (
    AXUIElementCreateApplication,
    AXUIElementCopyAttributeValue,
    kAXErrorSuccess,
    kAXChildrenAttribute,
    kAXRoleAttribute,
    kAXDescriptionAttribute,
    kAXValueAttribute,
)
from CoreFoundation import CFArrayGetCount, CFArrayGetValueAtIndex

logger = logging.getLogger("taey-ed")

# Expected URL domain patterns per platform
PLATFORM_DOMAINS = {
    "coursera": ["coursera.org"],
    "khan_academy": ["khanacademy.org"],
    "edx": ["edx.org", "learning.edx.org"],
    "udemy": ["udemy.com"],
}

# Chrome's address bar has this description in accessibility tree
CHROME_ADDRESS_BAR_DESC = "Address and search bar"
SAFARI_ADDRESS_BAR_ROLE = "AXTextField"


def get_browser_url(app_name: str) -> str:
    """
    Get the current URL from browser's address bar via accessibility API.

    Args:
        app_name: Browser process name (e.g., "Google Chrome", "Safari")

    Returns:
        URL string, or empty string if not found.
    """
    workspace = NSWorkspace.sharedWorkspace()
    target_app = None
    for app in workspace.runningApplications():
        if app_name.lower() in app.localizedName().lower():
            target_app = app
            break

    if not target_app:
        logger.debug(f"Browser '{app_name}' not found")
        return ""

    root = AXUIElementCreateApplication(target_app.processIdentifier())
    url = _find_url_in_tree(root, depth=0, max_depth=8)
    return url or ""


def _find_url_in_tree(element, depth: int, max_depth: int) -> str:
    """
    Walk accessibility tree looking for Chrome/Safari address bar.

    Chrome: AXTextField with description "Address and search bar", value = URL
    Safari: AXTextField with role "AXTextField" near top of window, value = URL
    """
    if depth > max_depth:
        return ""

    # Check role
    err, role = AXUIElementCopyAttributeValue(element, kAXRoleAttribute, None)
    role_str = str(role) if err == kAXErrorSuccess and role else ""

    # Look for text field that might be address bar
    if role_str == "AXTextField":
        err, desc = AXUIElementCopyAttributeValue(element, kAXDescriptionAttribute, None)
        desc_str = str(desc) if err == kAXErrorSuccess and desc else ""

        if CHROME_ADDRESS_BAR_DESC.lower() in desc_str.lower():
            err, val = AXUIElementCopyAttributeValue(element, kAXValueAttribute, None)
            if err == kAXErrorSuccess and val:
                url = str(val)
                if url and ("." in url or "://" in url):
                    return url

    # Skip web content area (don't search inside page DOM, only browser chrome)
    if role_str == "AXWebArea":
        return ""

    # Recurse into children
    err, children = AXUIElementCopyAttributeValue(element, kAXChildrenAttribute, None)
    if err == kAXErrorSuccess and children:
        count = CFArrayGetCount(children)
        for i in range(count):
            child = CFArrayGetValueAtIndex(children, i)
            result = _find_url_in_tree(child, depth + 1, max_depth)
            if result:
                return result

    return ""


def verify_browser_url(app_name: str, platform: str) -> dict:
    """
    Verify browser is on the expected platform domain.

    Args:
        app_name: Browser process name
        platform: Platform key (e.g., "coursera", "khan_academy")

    Returns:
        {"ok": True/False, "url": str, "expected_domains": list, "message": str}
    """
    expected = PLATFORM_DOMAINS.get(platform, [])
    if not expected:
        # No domain check configured for this platform
        return {"ok": True, "url": "", "expected_domains": [], "message": "no_check_configured"}

    url = get_browser_url(app_name)
    if not url:
        logger.warning(f"Could not read URL from {app_name}")
        return {
            "ok": False,
            "url": "",
            "expected_domains": expected,
            "message": f"Could not read URL from {app_name}",
        }

    # Normalize: Chrome sometimes shows URL without protocol
    url_lower = url.lower()
    if not url_lower.startswith("http"):
        url_lower = "https://" + url_lower

    # Check if URL contains any expected domain
    for domain in expected:
        if domain.lower() in url_lower:
            logger.info(f"URL verified: {url} matches {domain}")
            return {"ok": True, "url": url, "expected_domains": expected, "message": "ok"}

    logger.warning(f"URL mismatch: {url} does not match {expected}")
    return {
        "ok": False,
        "url": url,
        "expected_domains": expected,
        "message": f"Browser is on '{url}', expected one of: {expected}",
    }
