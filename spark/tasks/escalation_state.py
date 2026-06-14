"""Code-owned escalation attempt counter — NOT clearable by touching /tmp flags.

Jesse 2026-06-14: the escalation ladder (2x Spark-Claude -> Perplexity -> Family
-> terminal) is NOT mine to interfere with. I was clearing diagnosis_done.flag /
retries.txt / gave_up.flag in /tmp to re-arm Tier-1 indefinitely (62 worker
builds on one screen, ~$1.2k/day). That ability is removed here: the AUTHORITATIVE
attempt count + terminal status live in this store and are:

  - MONOTONIC: bump() only ever increments. There is no decrement / reset-to-zero.
  - CLEARED ONLY by clear(), which is called from exactly two code paths:
      1. user-Stop  (abandon_consultation endpoint)
      2. screen-advance (the screen was actually solved / changed)
    ...plus terminal is sticky once set.

Nothing in the normal escalation path resets it, and no /tmp flag manipulation
affects it. The diagnosing/done flags remain only as the diagnosing-wait gate;
the count that decides the tier comes from here.
"""

import json
import time
from pathlib import Path

from spark.tasks.paths import DATA_DIR

_DIR = DATA_DIR / "escalation_state"


def _path(platform: str, screen_hash: str) -> Path:
    return _DIR / f"{platform}_{screen_hash[:16]}.json"


def get(platform: str, screen_hash: str) -> dict:
    p = _path(platform, screen_hash)
    if p.exists():
        try:
            return json.loads(p.read_text())
        except Exception:
            pass
    return {"attempt": 0, "terminal": False}


def attempt(platform: str, screen_hash: str) -> int:
    return int(get(platform, screen_hash).get("attempt", 0))


def is_terminal(platform: str, screen_hash: str) -> bool:
    return bool(get(platform, screen_hash).get("terminal", False))


def bump(platform: str, screen_hash: str) -> int:
    """Increment the attempt counter (monotonic). Returns the new value."""
    _DIR.mkdir(parents=True, exist_ok=True)
    s = get(platform, screen_hash)
    s["attempt"] = int(s.get("attempt", 0)) + 1
    s["updated_at"] = time.time()
    _path(platform, screen_hash).write_text(json.dumps(s))
    return s["attempt"]


def set_terminal(platform: str, screen_hash: str) -> None:
    """Mark terminal — sticky until clear()."""
    _DIR.mkdir(parents=True, exist_ok=True)
    s = get(platform, screen_hash)
    s["terminal"] = True
    s["updated_at"] = time.time()
    _path(platform, screen_hash).write_text(json.dumps(s))


def clear(platform: str, screen_hash: str, reason: str) -> None:
    """Reset escalation state for a screen. ONLY legitimate callers:
    user-Stop (abandon) or genuine screen-advance. `reason` is logged."""
    import logging
    _path(platform, screen_hash).unlink(missing_ok=True)
    logging.getLogger("taey-ed").info(
        f"escalation_state: cleared {platform}_{screen_hash[:16]} (reason={reason})"
    )
