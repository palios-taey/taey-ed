"""
Send notification to Spark Claude (taey-ed) session.

Uses canonical taey-notify CLI via fire-and-forget Popen so FastAPI never blocks
on Redis or shell IO. Handles arbitrary-length payloads (BT bodies, debug logs)
without truncation — Redis carries the full envelope, the daemon delivers a
short [NOTIFY] pointer if the target is idle, and PostToolUse surfaces the full
content via additionalContext on the next tool call.

Pattern confirmed by conductor (2026-05-18). Replaces the previous tmux
load-buffer / paste-buffer / C-m approach which silently truncated long content.
"""

import logging
import subprocess

logger = logging.getLogger(__name__)

TAEY_NOTIFY_BIN = "/usr/local/bin/taey-notify"
TARGET = "taey-ed"
DEFAULT_TYPE = "escalation"
FROM_ID = "spark"


def notify_spark_claude(message: str, notify_type: str = DEFAULT_TYPE) -> bool:
    """
    Send a notification to the taey-ed session via the canonical Redis-backed
    notification system.

    Args:
        message: Arbitrary-length payload (BT JSON, debug tails, diagnoses).
        notify_type: One of message/task/directive/escalation/defect/
                     response_ready. Defaults to 'escalation' for CLAUDE_
                     DIAGNOSIS_REQUIRED-class messages.

    Returns:
        True if the subprocess was launched. Delivery is asynchronous; the CLI
        owns Redis envelope construction (timestamp, msg_id, normalized shape).
    """
    try:
        subprocess.Popen(
            [
                TAEY_NOTIFY_BIN,
                TARGET,
                "--type", notify_type,
                "--from", FROM_ID,
                message,
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        logger.info(
            f"Notified {TARGET} via taey-notify (type={notify_type}, "
            f"len={len(message)}): {message[:60]!r}"
        )
        return True
    except FileNotFoundError:
        logger.error(f"taey-notify binary not found at {TAEY_NOTIFY_BIN}")
        return False
    except Exception as e:
        logger.error(f"taey-notify dispatch failed: {e}")
        return False
