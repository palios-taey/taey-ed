# STATUS: FROZEN - Proven in v7. Verified 2026-02-19. Do not modify.
"""
Wait for element to appear in accessibility tree.
Polls with max_wait timeout to prevent infinite hang.
"""

import time
from .find_element import find_element


def wait_for_element(
    app_name: str,
    target_text: str,
    poll_interval: float = 0.5,
    max_wait: float = 60.0,
    role: str = None,
    match_mode: str = "exact",
):
    """
    Poll until element with target text appears, or timeout.

    Args:
        app_name: Application name (e.g., "Acellus")
        target_text: Text to wait for (e.g., "Next")
        poll_interval: Seconds between polls (default 0.5)
        max_wait: Maximum seconds to wait before raising (default 60)
        role: Optional AX role filter
        match_mode: "exact" or "contains"

    Returns:
        Raw AXUIElement reference when found

    Raises:
        RuntimeError: If element not found within max_wait seconds.
            This is element polling, not consultation — timeouts apply.
    """
    deadline = time.monotonic() + max_wait
    while time.monotonic() < deadline:
        try:
            element = find_element(app_name, target_text, role=role, match_mode=match_mode)
            if element is not None:
                return element
        except RuntimeError:
            # App not found or tree error - keep trying until deadline
            pass

        time.sleep(poll_interval)

    raise RuntimeError(
        f"Element '{target_text}' not found in {app_name} after {max_wait}s"
    )


if __name__ == "__main__":
    print("Waiting for 'Next' button...")
    element = wait_for_element("Acellus", "Next")
    print(f"Found: {element}")
