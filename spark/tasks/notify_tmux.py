# STATUS: FROZEN. Verified 2026-02-19. Do not modify.
"""
Send tmux notification to Spark Claude session.
Single-purpose file - FREEZE once working.

Pattern from v6: load-buffer + paste-buffer + C-m (Enter)
This injects message into the input box and submits it.
"""

import subprocess
import logging

logger = logging.getLogger(__name__)

TMUX_SESSION = "taey-ed"  # Dedicated Taey-Ed consultation session


def notify_spark_claude(message: str) -> bool:
    """
    Inject message into Spark Claude's tmux session and submit.

    Uses the proven v6 pattern:
    1. echo message | tmux load-buffer -  (load into buffer)
    2. tmux paste-buffer -t session       (paste into input)
    3. tmux send-keys -t session C-m      (press Enter to submit)

    Args:
        message: Message to inject and submit

    Returns:
        True if notification sent successfully
    """
    try:
        # Check if session exists
        result = subprocess.run(
            ["tmux", "has-session", "-t", TMUX_SESSION],
            capture_output=True,
            timeout=5
        )

        if result.returncode != 0:
            logger.warning(f"tmux session '{TMUX_SESSION}' not found")
            return False

        # Step 1: Load message into tmux buffer
        load_proc = subprocess.run(
            ["tmux", "load-buffer", "-"],
            input=message.encode(),
            capture_output=True,
            timeout=5
        )
        if load_proc.returncode != 0:
            logger.error(f"Failed to load buffer: {load_proc.stderr.decode()}")
            return False

        # Step 2: Paste buffer into session's input
        paste_proc = subprocess.run(
            ["tmux", "paste-buffer", "-t", TMUX_SESSION],
            capture_output=True,
            timeout=5
        )
        if paste_proc.returncode != 0:
            logger.error(f"Failed to paste buffer: {paste_proc.stderr.decode()}")
            return False

        # Pause to ensure paste completes before Enter
        import time
        time.sleep(0.5)

        # Step 3: Send Enter (C-m) to submit, twice with gap for reliability
        for attempt in range(2):
            send_proc = subprocess.run(
                ["tmux", "send-keys", "-t", TMUX_SESSION, "C-m"],
                capture_output=True,
                timeout=5
            )
            if send_proc.returncode != 0:
                logger.error(f"Failed to send Enter: {send_proc.stderr.decode()}")
                return False
            if attempt == 0:
                time.sleep(0.3)

        logger.info(f"Notified Spark Claude: {message[:60]}")
        return True

    except subprocess.TimeoutExpired:
        logger.error("tmux notification timed out")
        return False
    except Exception as e:
        logger.error(f"tmux notification failed: {e}")
        return False
