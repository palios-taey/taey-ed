"""Code-owned escalation coordination — not clearable by touching files.

Jesse 2026-06-14: the escalation ladder (2x Spark-Claude -> Perplexity -> Family
-> terminal) is NOT mine to interfere with. The authoritative attempt count,
diagnosis wait/resume state, dispatch dedup, and terminal status live in the
SQLite state store and are:

  - MONOTONIC: bump() only ever increments. There is no decrement / reset-to-zero.
  - CLEARED ONLY by clear(), which is called from exactly two code paths:
      1. user-Stop  (abandon_consultation endpoint)
      2. screen-advance (the screen was actually solved / changed)
    ...plus terminal is sticky once set.

Nothing in the normal escalation path resets it, and no /tmp flag manipulation
affects it.
"""

import logging
import time

logger = logging.getLogger(__name__)


def _state_evidence(source: str, **extra) -> dict:
    return {"source": f"escalation_state.{source}", **extra}


def _state_repo():
    from spark.state_repo import get_state_repo
    return get_state_repo()


def get(platform: str, screen_hash: str) -> dict:
    return _state_repo().get_ladder_state(platform=platform, screen_hash=screen_hash)


def is_terminal(platform: str, screen_hash: str) -> bool:
    return bool(get(platform, screen_hash).get("terminal", False))


# attempt() and bump() removed 2026-07-09 (cleanup-dead-apis): both dead —
# superseded by note_attempt(), which is the ONLY legitimate ladder-climb
# (once per distinct failed attempt key, never on a timer).


def note_attempt(platform: str, screen_hash: str, consult_id: str) -> int:
    """Advance the ladder ONLY on a genuinely distinct failed attempt.

    Jesse/operator 2026-06-14: the ladder must climb on real attempts, not on a
    blind timer. The auto-resume timer was bumping the tier every few minutes —
    so a screen being actively fixed got steamrolled tier1->2->3->terminal while
    the operator was correctly holding for an in-flight DR. The attempt counter
    now advances here, once per DISTINCT failed attempt key: consult id for a
    worker attempt, directive id for a Mac-executed BT failure. Re-reading the
    same failed key does NOT climb; missing keys and timers do NOT climb.
    Returns the current attempt count.
    """
    return _state_repo().record_escalation_attempt(
        platform=platform,
        screen_hash=screen_hash,
        consult_id=consult_id or "",
        actor="api",
        evidence=_state_evidence("note_attempt", consult_id=consult_id or ""),
    )


def set_terminal(platform: str, screen_hash: str) -> None:
    """Mark terminal — sticky until clear()."""
    _state_repo().mark_terminal(
        platform=platform,
        screen_hash=screen_hash,
        actor="api",
        evidence=_state_evidence("set_terminal"),
    )


def clear(platform: str, screen_hash: str, reason: str) -> None:
    """Reset escalation state for a screen. ONLY legitimate callers:
    user-Stop (abandon) or genuine screen-advance. `reason` is logged."""
    _state_repo().clear_ladder(
        platform=platform,
        screen_hash=screen_hash,
        reason=reason,
        actor="api",
        evidence=_state_evidence("clear", reason=reason),
    )
    logging.getLogger("taey-ed").info(
        f"escalation_state: cleared {platform}_{screen_hash[:16]} (reason={reason})"
    )


def clear_platform(platform: str, reason: str) -> int:
    """Clear ALL escalation state for a platform. ONLY legitimate caller:
    user-Stop full session reset (/session/reset). Added 2026-07-09
    (cleanup-dead-apis) so the reset route stops raw-unlinking this store's
    files around the module API. Returns the number of entries cleared."""
    cleared = _state_repo().clear_platform_ladders(
        platform=platform,
        reason=reason,
        actor="api",
        evidence=_state_evidence("clear_platform", reason=reason),
    )
    logging.getLogger("taey-ed").info(
        f"escalation_state: cleared ALL {cleared} entries for {platform} (reason={reason})"
    )
    return cleared


def start_diagnosis_cycle(platform: str, screen_hash: str, tier: str, window_seconds: int) -> bool:
    resume_at_ms = int((time.time() + window_seconds) * 1000)
    return _state_repo().start_diagnosis_cycle(
        platform=platform,
        screen_hash=screen_hash,
        tier=tier,
        resume_at_ms=resume_at_ms,
        response_pending_until_ms=resume_at_ms,
        actor="api",
        evidence=_state_evidence("start_diagnosis_cycle", tier=tier, window_seconds=window_seconds),
    )


def list_active_diagnoses(limit: int = 16) -> list[dict]:
    return _state_repo().list_active_diagnoses(limit=limit)


def resume_diagnosis_cycle(platform: str, screen_hash: str, reason: str) -> bool:
    return _state_repo().resume_diagnosis_cycle(
        platform=platform,
        screen_hash=screen_hash,
        actor="api",
        evidence=_state_evidence("resume_diagnosis_cycle", reason=reason),
    )


def dispatch_tier_once(platform: str, screen_hash: str, tier: str) -> bool:
    return _state_repo().dispatch_once_for_ladder(
        platform=platform,
        screen_hash=screen_hash,
        tier=tier,
        actor="api",
        evidence=_state_evidence("dispatch_tier_once", tier=tier),
    )


def knowledge_gate_notify_once(platform: str) -> bool:
    return _state_repo().knowledge_gate_notify_once(
        platform=platform,
        actor="api",
        evidence=_state_evidence("knowledge_gate_notify_once"),
    )


def clear_knowledge_gate(platform: str) -> bool:
    return _state_repo().clear_knowledge_gate(
        platform=platform,
        actor="api",
        evidence=_state_evidence("clear_knowledge_gate"),
    )


def list_pending_consults(limit: int = 16) -> list[dict]:
    return _state_repo().list_pending_consults(limit=limit)


def abandon_pending_consults_for_screen(platform: str, screen_hash: str, reason: str) -> int:
    return _state_repo().abandon_pending_consults_for_screen(
        platform=platform,
        screen_hash=screen_hash,
        reason=reason,
        actor="api",
        evidence=_state_evidence("abandon_pending_consults_for_screen", reason=reason),
    )


def abandon_pending_consults_for_platform(platform: str, reason: str) -> int:
    return _state_repo().abandon_pending_consults_for_platform(
        platform=platform,
        reason=reason,
        actor="api",
        evidence=_state_evidence("abandon_pending_consults_for_platform", reason=reason),
    )
