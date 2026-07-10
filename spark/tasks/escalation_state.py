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
import logging
import time
from pathlib import Path

from spark.tasks.paths import DATA_DIR

_DIR = DATA_DIR / "escalation_state"
logger = logging.getLogger(__name__)


def _state_evidence(source: str, **extra) -> dict:
    return {"source": f"escalation_state.{source}", **extra}


def _state_repo():
    from spark.state_repo import get_state_repo
    return get_state_repo()


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


def is_terminal(platform: str, screen_hash: str) -> bool:
    return bool(get(platform, screen_hash).get("terminal", False))


# attempt() and bump() removed 2026-07-09 (cleanup-dead-apis): both dead —
# superseded by note_attempt(), which is the ONLY legitimate ladder-climb
# (once per distinct failed consult id, never on a timer).


def note_attempt(platform: str, screen_hash: str, consult_id: str) -> int:
    """Advance the ladder ONLY on a genuinely distinct failed attempt.

    Jesse/operator 2026-06-14: the ladder must climb on real attempts, not on a
    blind timer. The auto-resume timer was bumping the tier every few minutes —
    so a screen being actively fixed got steamrolled tier1->2->3->terminal while
    the operator was correctly holding for an in-flight DR. The attempt counter
    now advances here, once per DISTINCT failed consult id (a real worker
    attempt that failed). Re-reading the same failed consult does NOT climb; the
    timer does NOT climb. Returns the current attempt count.
    """
    _DIR.mkdir(parents=True, exist_ok=True)
    s = get(platform, screen_hash)
    if consult_id and s.get("last_failed_consult") == consult_id:
        return int(s.get("attempt", 0))   # already counted this attempt
    s["attempt"] = int(s.get("attempt", 0)) + 1
    s["last_failed_consult"] = consult_id or ""
    s["updated_at"] = time.time()
    _path(platform, screen_hash).write_text(json.dumps(s))
    try:
        _state_repo().record_escalation_attempt(
            platform=platform,
            screen_hash=screen_hash,
            consult_id=consult_id or "",
            actor="api",
            evidence=_state_evidence("note_attempt", consult_id=consult_id or ""),
        )
    except Exception:
        logger.exception("state-store dual-write failed: escalation_state.note_attempt")
    return s["attempt"]


def set_terminal(platform: str, screen_hash: str) -> None:
    """Mark terminal — sticky until clear()."""
    _DIR.mkdir(parents=True, exist_ok=True)
    s = get(platform, screen_hash)
    s["terminal"] = True
    s["updated_at"] = time.time()
    _path(platform, screen_hash).write_text(json.dumps(s))
    try:
        _state_repo().mark_terminal(
            platform=platform,
            screen_hash=screen_hash,
            actor="api",
            evidence=_state_evidence("set_terminal"),
        )
    except Exception:
        logger.exception("state-store dual-write failed: escalation_state.set_terminal")


def clear(platform: str, screen_hash: str, reason: str) -> None:
    """Reset escalation state for a screen. ONLY legitimate callers:
    user-Stop (abandon) or genuine screen-advance. `reason` is logged."""
    import logging
    _path(platform, screen_hash).unlink(missing_ok=True)
    try:
        _state_repo().clear_ladder(
            platform=platform,
            screen_hash=screen_hash,
            reason=reason,
            actor="api",
            evidence=_state_evidence("clear", reason=reason),
        )
    except Exception:
        logger.exception("state-store dual-write failed: escalation_state.clear")
    logging.getLogger("taey-ed").info(
        f"escalation_state: cleared {platform}_{screen_hash[:16]} (reason={reason})"
    )


def clear_platform(platform: str, reason: str) -> int:
    """Clear ALL escalation state for a platform. ONLY legitimate caller:
    user-Stop full session reset (/session/reset). Added 2026-07-09
    (cleanup-dead-apis) so the reset route stops raw-unlinking this store's
    files around the module API. Returns the number of entries cleared."""
    import logging
    cleared = 0
    if _DIR.exists():
        for f in _DIR.glob(f"{platform}_*.json"):
            f.unlink(missing_ok=True)
            cleared += 1
    try:
        _state_repo().clear_platform_ladders(
            platform=platform,
            reason=reason,
            actor="api",
            evidence=_state_evidence("clear_platform", reason=reason),
        )
    except Exception:
        logger.exception("state-store dual-write failed: escalation_state.clear_platform")
    logging.getLogger("taey-ed").info(
        f"escalation_state: cleared ALL {cleared} entries for {platform} (reason={reason})"
    )
    return cleared
