"""
Synthetic-input event routing.

Per Jesse 2026-06-01, macOS 14+/15+/26.4+ cooperative activation rule:
a background app cannot activate another app, and CGEventPost(kCGHIDEventTap)
only delivers events to the macOS frontmostApplication. The earlier
_activate_ctx_app / _activate_element_app path uses NSWorkspace
activateWithOptions_ which returns success while silently no-opping.
The net effect was synthetic clicks/keys landing in whatever app was
frontmost (Screen Sharing, the terminal, an editor, anything) instead
of Chrome — and handlers reporting phantom success.

Two paths to fix it:

  A) DETECT-AND-FAIL-LOUDLY for KEYBOARD events. assert_target_frontmost()
     raises TargetNotFrontmostError before posting any keyboard event whose
     target app is not frontmost. Caller surfaces a clean BT failure instead
     of phantom success. Server-side BT engine can then escalate
     (Tier 3 user prompt: "click on Chrome and resume"). Keyboard events
     MUST land in the frontmost app — there is no PID routing for them
     because Chrome's intra-window focus shim only delivers to its
     focused element when Chrome is the macOS-frontmost app.

  B) CGEventPostToPid for COORDINATE events (mouse clicks, drags, scrolls).
     Posts the event DIRECTLY to a target PID, bypassing the HID-tap's
     frontmost routing. Coordinate events resolve at the window-server
     level by absolute screen coordinates, so the click lands at the
     right pixel in Chrome's window regardless of who has focus. This is
     what unblocks the find_and_click 'Next question' phantom-success case.

Both helpers are best-effort — if the app isn't running, we fall back
to the HID tap (B) or raise a clear error (A). Never silently no-op.
"""

import logging
import subprocess
import time
from typing import Optional

from AppKit import NSWorkspace
from Quartz import (
    CGEventPost,
    CGEventPostToPid,
    kCGHIDEventTap,
)

logger = logging.getLogger("taey-ed")


class TargetNotFrontmostError(RuntimeError):
    """Synthetic keyboard event was about to fire but the target app is
    not macOS-frontmost. Posting would route the event to the wrong app
    and produce phantom success. Raised by assert_target_frontmost so
    callers surface a clean BT failure."""


class NeedsAutomationGrantError(RuntimeError):
    """AppleScript activation of the target app failed with macOS error
    -1743 (errAEEventNotPermitted). The user must grant the Automation
    permission for Taey-Ed → <target app> in System Settings →
    Privacy & Security → Automation. Surfaces a clean self-service
    message in the error string."""


def _norm(name: Optional[str]) -> str:
    return (name or "").strip().lower()


def is_target_frontmost(app_name: str) -> bool:
    """True iff the running app whose localizedName contains app_name
    (case-insensitive substring) is the macOS frontmostApplication."""
    if not app_name:
        return False
    front = NSWorkspace.sharedWorkspace().frontmostApplication()
    if not front:
        return False
    return _norm(app_name) in _norm(front.localizedName())


def assert_target_frontmost(app_name: str) -> None:
    """Raise TargetNotFrontmostError if app_name is not frontmost.

    Use BEFORE posting a synthetic keyboard event (press_key, type_keys,
    press_escape, focus_enter post-AX-focus, focus_space post-AX-focus).
    Mouse / scroll events do NOT need this — use CGEventPostToPid via
    post_coord_event_to_app instead.
    """
    if is_target_frontmost(app_name):
        return
    front = NSWorkspace.sharedWorkspace().frontmostApplication()
    front_name = (front.localizedName() if front else "(none)") or "(none)"
    raise TargetNotFrontmostError(
        f"input target {app_name!r} not frontmost (frontmost is "
        f"{front_name!r}); macOS cooperative-activation blocked it; "
        f"cannot send keyboard input"
    )


def find_app_pid(app_name: str) -> Optional[int]:
    """Return PID of the running app whose localizedName contains
    app_name (case-insensitive substring), else None."""
    if not app_name:
        return None
    target = _norm(app_name)
    for app in NSWorkspace.sharedWorkspace().runningApplications():
        if target in _norm(app.localizedName()):
            return int(app.processIdentifier())
    return None


def activate_via_applescript(app_name: str, timeout: float = 1.5) -> bool:
    """Bring app_name to macOS-frontmost via osascript Apple Event.

    Per Jesse 2026-06-01 (Path 2): macOS 14+/15+/26.4+ cooperative
    activation rule blocks NSWorkspace.activateWithOptions_ from working
    in background contexts. AppleScript 'tell application X to activate'
    works because Apple Events go through the TCC Automation grant path,
    which is a user-authorized override of the cooperative-activation
    rule. Requires:
      - NSAppleEventsUsageDescription in Info.plist (yes, set in setup.py)
      - User-granted Automation permission for Taey-Ed → <app_name>
        under System Settings → Privacy & Security → Automation

    Returns True if osascript reported success and the app is now
    frontmost. Returns False if osascript reported success but
    frontmost still isn't the target (transient race).

    Raises NeedsAutomationGrantError if osascript failed with -1743
    (errAEEventNotPermitted) — the actionable case where the user
    must grant Automation in System Settings.

    Constitutional note: this is ONLY for window foregrounding. All
    synthetic input continues to go through AX + CGEvent, never
    through AppleScript / Apple Events. The page (or any anti-bot
    fingerprinting) cannot observe HOW the OS window was activated —
    AppleScript-activate is indistinguishable from a Dock click.
    """
    if not app_name:
        return False
    if is_target_frontmost(app_name):
        return True

    # Escape double-quotes in the app name for the osascript literal.
    safe = app_name.replace('\\', '\\\\').replace('"', '\\"')
    script = f'tell application "{safe}" to activate'
    try:
        r = subprocess.run(
            ["osascript", "-e", script],
            capture_output=True, text=True, timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        logger.warning(
            f"activate_via_applescript({app_name!r}): osascript timed out"
        )
        return False
    if r.returncode == 0:
        # Give the window-server a moment to honor the activation.
        time.sleep(0.15)
        return is_target_frontmost(app_name)

    stderr = (r.stderr or "").strip()
    if "-1743" in stderr or "not allowed" in stderr.lower():
        raise NeedsAutomationGrantError(
            f"Automation permission missing for Taey-Ed → {app_name!r}. "
            f"Grant under System Settings > Privacy & Security > "
            f"Automation > Taey-Ed > {app_name}. "
            f"(osascript stderr: {stderr})"
        )
    logger.warning(
        f"activate_via_applescript({app_name!r}): osascript failed "
        f"rc={r.returncode} stderr={stderr!r}"
    )
    return False


def ensure_target_frontmost(app_name: str) -> None:
    """Bring app_name to frontmost (via AppleScript Apple Event) if it
    isn't already, then assert. Raise on failure.

    Combined helper called by KEYBOARD handlers before they post synthetic
    events. The two-step:
      1. If already frontmost, return.
      2. Try activate_via_applescript; on success, return.
      3. Fall back to assert_target_frontmost (which raises with the
         standard 'not frontmost' message naming the actual frontmost).

    If the Automation grant is missing, activate_via_applescript raises
    NeedsAutomationGrantError, which propagates with a self-service
    System-Settings-path message.
    """
    if is_target_frontmost(app_name):
        return
    # May raise NeedsAutomationGrantError on -1743; let it propagate.
    if activate_via_applescript(app_name):
        return
    # Activation didn't take (transient or non-Automation error). Defer
    # to the standard assert which raises with the named frontmost.
    assert_target_frontmost(app_name)


def post_coord_event_to_app(event, app_name: str) -> bool:
    """Post a CGEvent (mouse / scroll / drag) DIRECTLY to app_name's PID
    via CGEventPostToPid. Routes regardless of who is frontmost.

    Returns True if the event was routed to a PID, False if the app
    wasn't found and we fell back to kCGHIDEventTap.
    """
    pid = find_app_pid(app_name)
    if pid is None:
        logger.warning(
            f"post_coord_event_to_app: {app_name!r} not running; "
            f"falling back to kCGHIDEventTap"
        )
        CGEventPost(kCGHIDEventTap, event)
        return False
    CGEventPostToPid(pid, event)
    return True
