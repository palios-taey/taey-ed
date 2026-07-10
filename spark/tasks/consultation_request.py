"""
Consultation request handling.

Creates and checks consultation requests for unknown screens.
Includes knowledge gate: no knowledge.json = research-first notification.
"""

import base64
import json
import logging
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import List

from .atomic_write import atomic_write_json
from .consultation_state import (
    ConsultationState,
    get_consultation_state,
    set_consultation_state,
    compute_tree_hash,
)
from .notify_tmux import notify_spark_claude
from .paths import is_valid_png_b64
from .escalation import (
    tier_for_attempt,
    build_packet,
    notify_body_for_tier,
    dispatch_body_for_tier,
    notify_fleet,
    UNSOLVED_LOG,
)
from spark.worker.consultation_worker import use_worker_enabled

logger = logging.getLogger(__name__)

CONSULT_DIR = Path("/tmp/taey-ed-consult")

# Pending consultations that exceed this age are auto-abandoned to prevent
# the ONE-AT-A-TIME gate from blocking forever when Mac dies (kill -9, panic,
# crash before sending /abandon_consultation). 10 minutes is much longer than
# any normal Mac→Spark Claude round-trip but short enough that a crashed Mac
# self-heals before the user gives up.
PENDING_TTL_SECONDS = 600

# ONE consultation at a time. Period.
# If one is pending, every code path returns it instead of creating another.


def _state_evidence(source: str, **extra) -> dict:
    return {"source": f"consultation_request.{source}", **extra}


def _state_repo():
    from spark.state_repo import get_state_repo
    return get_state_repo()


def _coordination_screen_hash(tree: dict) -> str:
    try:
        from spark.tasks.skeleton import extract_skeleton, skeleton_hash
        return skeleton_hash(extract_skeleton(tree))
    except Exception:
        return compute_tree_hash(tree)


def _mirror_open_consult(metadata: dict, consult_path: Path, source: str) -> None:
    try:
        _state_repo().open_consult(
            consult_id=metadata["consultation_id"],
            platform=metadata["platform"],
            screen_hash=metadata.get("coordination_screen_hash") or metadata.get("screen_hash"),
            payload_dir=str(consult_path),
            actor="api",
            evidence=_state_evidence(source),
        )
    except Exception:
        logger.exception("state-store dual-write failed: consultation_request.%s", source)


def _mirror_resolve_consult(metadata: dict, status: str, source: str, reason: str | None = None) -> None:
    try:
        _state_repo().resolve_consult(
            consult_id=metadata["consultation_id"],
            status=status,
            actor="api",
            evidence=_state_evidence(source),
            abandon_reason=reason if status == "abandoned" else None,
            failure_reason=reason if status == "worker_failed" else None,
        )
    except Exception:
        logger.exception("state-store dual-write failed: consultation_request.%s", source)


def _mirror_consult_status(metadata: dict, status: str, source: str, payload: dict | None = None) -> None:
    try:
        _state_repo().record_consult_status_event(
            consult_id=metadata["consultation_id"],
            platform=metadata["platform"],
            screen_hash=metadata.get("coordination_screen_hash") or metadata.get("screen_hash"),
            status=status,
            actor="api",
            evidence=_state_evidence(source),
            payload=payload,
        )
    except Exception:
        logger.exception("state-store dual-write failed: consultation_request.%s", source)


def _require_worker_mode() -> None:
    if not use_worker_enabled():
        raise RuntimeError(
            "TAEY_ED_USE_WORKER is required for consultation builds; tmux/primary fallback is disabled"
        )


def _pending_consult_is_blocking(meta: dict, consult_path: Path) -> bool:
    """Return True if this metadata represents a pending consult that should
    block new consultation creation. Auto-abandons stale pending consults
    (timestamp older than PENDING_TTL_SECONDS) by writing status=abandoned
    back to disk, matching the explicit /abandon_consultation endpoint behavior.
    """
    if meta.get("status") != "pending":
        return False  # complete / abandoned / unknown — non-blocking
    ts = meta.get("timestamp", "")
    if not ts:
        return True  # no timestamp, treat as blocking conservatively
    try:
        consult_time = datetime.fromisoformat(ts)
    except Exception:
        return True
    age = (datetime.now() - consult_time).total_seconds()
    if age <= PENDING_TTL_SECONDS:
        return True  # fresh pending — blocks
    # Stale pending — auto-abandon
    meta["status"] = "abandoned"
    meta["abandoned_at"] = datetime.now().isoformat()
    meta["abandoned_reason"] = f"ttl_expired age={int(age)}s"
    try:
        atomic_write_json(consult_path / "metadata.json", meta)
        _mirror_resolve_consult(meta, "abandoned", "ttl_auto_abandon", meta.get("abandoned_reason"))
        logger.warning(
            f"Auto-abandoned stale pending consult "
            f"{meta.get('consultation_id', consult_path.name)} (age={int(age)}s)"
        )
    except Exception as e:
        logger.warning(f"Failed to write auto-abandon metadata: {e}")
    return False  # stale → no longer blocks


def request_consultation(
    platform: str,
    tree: dict,
    screenshot_b64: str,
    context: dict,
    bt_debug_log: str = "",
) -> dict:
    """
    Save consultation request for Spark Claude review.

    Args:
        platform: Platform name (e.g., "khan_academy")
        tree: Accessibility tree in macapptree format
        screenshot_b64: Base64-encoded screenshot
        context: Additional context (previous_screen, action_taken, etc.)
        bt_debug_log: Behavior tree execution trace from Mac

    Returns:
        {"consultation_id": str, "status": "pending"|"existing"|"user_required"}
    """
    _require_worker_mode()
    CONSULT_DIR.mkdir(parents=True, exist_ok=True)

    # ONE AT A TIME: If any consultation is pending AND not yet responded AND
    # not stale (TTL), return it. A consultation with response.json on disk is
    # effectively complete even if metadata.status was never flipped (Spark
    # Claude writes the response file directly without going through the API).
    # Status=="abandoned" (set by /abandon_consultation endpoint or TTL) is
    # treated as terminal — does not block new consultations.
    for _p in CONSULT_DIR.iterdir():
        if not _p.is_dir() or not _p.name.startswith("consult_"):
            continue
        if (_p / "response.json").exists():
            continue
        _mf = _p / "metadata.json"
        if _mf.exists():
            try:
                _m = json.loads(_mf.read_text())
                if _pending_consult_is_blocking(_m, _p):
                    existing_id = _m.get("consultation_id", "")
                    logger.info(
                        f"Consultation already pending: {existing_id}. "
                        f"Returning existing instead of creating new."
                    )
                    return {
                        "consultation_id": existing_id,
                        "status": "existing",
                        "message": f"Waiting on existing consultation {existing_id}",
                    }
            except Exception:
                continue

    consultation_id = f"consult_{int(time.time())}_{uuid.uuid4().hex[:8]}"
    consult_path = CONSULT_DIR / consultation_id
    consult_path.mkdir(parents=True, exist_ok=True)

    # Save screenshot only if it's a real PNG. Reject test/stale payloads
    # (e.g. screenshot_b64="test" decodes to 3 garbage bytes) loudly so we
    # never feed Claude a corrupt image and trigger an API 400.
    if screenshot_b64:
        if is_valid_png_b64(screenshot_b64):
            (consult_path / "screenshot.png").write_bytes(base64.b64decode(screenshot_b64))
        else:
            logger.error(
                f"Rejected screenshot_b64 for consult {consultation_id}: "
                f"not a valid PNG (len={len(screenshot_b64)}). "
                f"No screenshot.png written."
            )

    # Save tree (atomic to prevent Mac reading partial JSON during poll)
    atomic_write_json(consult_path / "tree.json", tree)

    # Save BT debug log (behavior tree execution trace from Mac)
    if bt_debug_log:
        (consult_path / "bt_debug.log").write_text(bt_debug_log)

    # Determine escalation level based on reconsultation history
    is_reconsultation = context.get("reconsultation", False)
    escalation_level = "spark_claude"
    spark_attempts = 0

    if is_reconsultation:
        current_hash = compute_tree_hash(tree)
        for prev_path in CONSULT_DIR.iterdir():
            if not prev_path.is_dir() or not prev_path.name.startswith("consult_"):
                continue
            prev_meta_file = prev_path / "metadata.json"
            if not prev_meta_file.exists():
                continue
            try:
                prev_meta = json.loads(prev_meta_file.read_text())
                if (prev_meta.get("platform") == platform
                        and prev_meta.get("consultation_id") != consultation_id
                        and prev_meta.get("screen_hash") == current_hash
                        and (prev_path / "response.json").exists()):
                    spark_attempts += 1
            except Exception:
                continue

        # ONE SHOT RULE (Jesse 2026-05-18): Any reconsultation = immediate
        # claude_diagnosing escalation. Worker gets exactly one attempt on a
        # screen. If the BT failed and Mac is asking again, the worker has
        # nothing new to try — needs claude to edit knowledge.json. The
        # spark_attempts counter is unreliable (screen_hash drift across
        # reconsults can keep it at 0 forever), so we trust is_reconsultation
        # as the escalation trigger.
        escalation_level = "user"  # routed through claude_diagnosing below
        logger.info(
            f"Reconsultation detected ({spark_attempts} prior counted) "
            f"→ escalation_level=user (one-shot rule)"
        )

    # Save context/metadata
    kb_payload = []
    for ch in (context.get("relevant_kb_chunks") or []):
        if hasattr(ch, "model_dump"):
            kb_payload.append(ch.model_dump())
        elif isinstance(ch, dict):
            kb_payload.append(ch)

    coordination_screen_hash = _coordination_screen_hash(tree)

    metadata = {
        "consultation_id": consultation_id,
        "platform": platform,
        "screen_type_hint": context.get("screen_type") or context.get("screen_type_hint") or "UNKNOWN",
        "failure_reason": context.get("failure_reason", ""),
        "previous_screen_type": context.get("previous_screen", ""),
        "screen_hash": compute_tree_hash(tree),
        "coordination_screen_hash": coordination_screen_hash,
        "context": context,
        "timestamp": datetime.now().isoformat(),
        "status": "pending",
        "escalation_level": escalation_level,
        "spark_attempts": spark_attempts,
        "relevant_kb_chunks": kb_payload,
    }
    atomic_write_json(consult_path / "metadata.json", metadata)
    _mirror_open_consult(metadata, consult_path, "request_consultation")

    # Track state
    set_consultation_state(consultation_id, ConsultationState(
        consultation_id=consultation_id,
        screen_hash=compute_tree_hash(tree),
        platform=platform,
    ))

    # Check if knowledge.json exists for this platform
    knowledge_path = (
        Path(__file__).parent.parent / "platforms" / platform / "knowledge.json"
    )
    needs_research = not knowledge_path.exists()

    if needs_research:
        metadata["research_required"] = True
        atomic_write_json(consult_path / "metadata.json", metadata)

    # (Legacy perplexity/user thresholds collapsed into the single
    # spark_attempts >= 1 → user check above. Kept here as a final safety net
    # in case escalation_level was set by a path that bypasses the above.)
    if spark_attempts >= 1 and escalation_level != "user":
        escalation_level = "user"

    # Hit user-escalation threshold. Before surfacing to the human user, run
    # the claude-diagnosis loop: notify the Mira-side Claude session, pause Mac
    # (return a wait status), let claude edit knowledge.json, then auto-retry.
    # State is keyed by (platform, screen_hash) at a stable path so it persists
    # across consult_id changes during reconsult cycles.
    if escalation_level == "user":
        screen_hash = metadata.get("coordination_screen_hash") or _coordination_screen_hash(tree)
        from spark.tasks import escalation_state

        diag_state_dir = Path("/tmp/taey-ed-claude-diagnosing") / f"{platform}_{screen_hash[:16]}"
        diag_state_dir.mkdir(parents=True, exist_ok=True)

        # Real user escalation: the DB-backed ladder is terminal.
        if escalation_state.is_terminal(platform, screen_hash):
            logger.warning(
                f"Escalation ladder is terminal for screen_hash={screen_hash[:16]}. "
                f"Escalating to user."
            )
            metadata["escalation_level"] = "user"
            metadata["status"] = "user_required"
            atomic_write_json(consult_path / "metadata.json", metadata)
            _mirror_consult_status(
                metadata,
                "user_required",
                "terminal_user_required",
                {"reason": "terminal_ladder"},
            )
            notify_spark_claude(
                f"ESCALATION TO USER: Consultation {consultation_id} for {platform} "
                f"— escalation ladder is terminal. User input required."
            )
            return {
                "consultation_id": consultation_id,
                "status": "user_required",
                "message": f"Exhausted {spark_attempts} attempts + claude diagnosis. User input needed.",
                "path": str(consult_path),
            }

        retry_count = max(0, escalation_state.note_attempt(platform, screen_hash, consultation_id) - 1)

        # Resolve which tier this attempt belongs to (see escalation.py for ladder).
        tier = tier_for_attempt(retry_count)

        # Terminal tier: full ladder exhausted. Auto-mark unsolvable, log to
        # UNSOLVED.md, return user_required. claude-primary does not give up
        # manually — the system does, per Jesse 2026-05-18 ladder.
        if tier == "terminal":
            logger.warning(
                f"Escalation ladder exhausted ({retry_count} cycles) for "
                f"screen_hash={screen_hash[:16]}. Auto-marking unsolvable."
            )
            escalation_state.set_terminal(platform, screen_hash)
            try:
                UNSOLVED_LOG.parent.mkdir(parents=True, exist_ok=True)
                with UNSOLVED_LOG.open("a") as fh:
                    fh.write(
                        f"\n## {datetime.utcnow().isoformat()}Z — {platform} {screen_hash[:16]}\n"
                        f"- consultation_id: {consultation_id}\n"
                        f"- attempts_exhausted: {retry_count}\n"
                        f"- state_dir: {diag_state_dir}\n"
                        f"- last_consult: {consult_path}\n"
                    )
            except Exception as e:
                logger.error(f"UNSOLVED.md append failed: {e}")
            metadata["escalation_level"] = "terminal"
            metadata["status"] = "user_required"
            atomic_write_json(consult_path / "metadata.json", metadata)
            _mirror_consult_status(
                metadata,
                "user_required",
                "terminal_user_required",
                {"retry_count": retry_count},
            )
            notify_spark_claude(
                f"TERMINAL ESCALATION — {platform} screen_hash {screen_hash[:16]} "
                f"marked unsolvable after ladder exhaustion. "
                f"Logged to {UNSOLVED_LOG}.",
                notify_type="defect",
            )
            return {
                "consultation_id": consultation_id,
                "status": "user_required",
                "message": f"Escalation ladder exhausted ({retry_count} cycles). Marked unsolvable.",
                "path": str(consult_path),
            }

        # First entry to diagnosing for this cycle, or already-pending.
        # Notify once per DB-backed cycle; build the rich-context escalation packet
        # so the recipient (claude-primary) and any Tier 2/3 dispatch has
        # everything it needs in one document.
        window = {"tier1": 180, "tier2": 1200, "tier3": 1200}.get(tier, 300)
        if escalation_state.start_diagnosis_cycle(platform, screen_hash, tier, window):
            screen_type_hint = metadata.get("screen_type_hint", "UNKNOWN")

            # Build the packet. Operational notes rendering deferred to
            # caller knowledge if available; here we pass empty string and
            # let the worker prompt include them at BT-gen time.
            knowledge = {}
            notes_md = ""

            try:
                packet_path = build_packet(
                    platform=platform,
                    screen_hash=screen_hash,
                    consult_path=consult_path,
                    diag_state_dir=diag_state_dir,
                    retry_count=retry_count,
                    knowledge=knowledge,
                    operational_notes_rendered=notes_md,
                    screen_type_hint=screen_type_hint,
                )
            except Exception as e:
                logger.error(f"escalation packet build failed: {e}")
                packet_path = diag_state_dir / "(packet_build_failed)"

            body = notify_body_for_tier(
                tier=tier,
                packet_path=packet_path,
                platform=platform,
                screen_hash=screen_hash,
                retry_count=retry_count,
                consult_path=consult_path,
                diag_state_dir=diag_state_dir,
            )
            notify_spark_claude(body, notify_type="escalation")

            # Auto-climb (INTENDED_FLOW §D): Tier 2/3 dispatch goes to
            # taeys-hands DIRECTLY from the server. claude-primary's
            # notification above is the synthesis/fold assignment, not a
            # relay instruction.
            dispatch_body = dispatch_body_for_tier(
                tier=tier,
                packet_path=packet_path,
                platform=platform,
                screen_hash=screen_hash,
                retry_count=retry_count,
                bt_debug_tail=bt_debug_log,
            )
            if dispatch_body:
                from spark.tasks.paths import REVIEWS_DIR as _REVIEWS_DIR
                review_path = _REVIEWS_DIR / f"{platform}_{screen_hash[:12]}_{tier}.md"
                if review_path.exists():
                    logger.info(
                        f"taeys-hands dispatch SKIPPED for {screen_hash[:16]} {tier} "
                        f"(review already landed — no-op re-fire dedup)"
                    )
                elif escalation_state.dispatch_tier_once(platform, screen_hash, tier):
                    notify_fleet("taeys-hands", dispatch_body, notify_type="task")
                else:
                    logger.info(
                        f"taeys-hands dispatch SKIPPED for {screen_hash[:16]} {tier} "
                        f"(already dispatched this ladder cycle)"
                    )
            logger.warning(
                f"Escalation triggered for {consultation_id} "
                f"({platform}, {screen_type_hint}, hash={screen_hash[:16]}, "
                f"tier={tier}, retry_count={retry_count}, "
                f"auto_dispatched={'yes' if dispatch_body else 'n/a'})"
            )
        metadata["status"] = "claude_diagnosing"
        metadata["escalation_level"] = f"diagnosing_{tier}"
        atomic_write_json(consult_path / "metadata.json", metadata)
        _mirror_consult_status(
            metadata,
            "claude_diagnosing",
            "diagnosing",
            {"tier": tier, "retry_count": retry_count},
        )
        return {
            "consultation_id": consultation_id,
            "status": "claude_diagnosing",
            "message": f"Escalation tier={tier} active — Mac will retry automatically.",
            "path": str(consult_path),
        }

    logger.info(
        f"Consultation created: {consultation_id} (worker mode — no tmux notify)"
    )

    # Rolling cleanup: keep only 2 most recent completed consultations
    _cleanup_old_consultations(keep=2)

    return {
        "consultation_id": consultation_id,
        "status": "pending",
        "message": "Worker picks up via poll",
        "path": str(consult_path),
    }


def request_minimal_consultation(
    platform: str,
    tree: dict,
    screenshot_b64: str,
    screen_type: str = "UNKNOWN",
    user_guidance: str | None = None,
    relevant_kb_chunks: list | None = None,
) -> dict:
    """
    Bypass-Gemini consultation for Claude-primary platforms.

    Saves tree + screenshot to /tmp/taey-ed-consult/{id}/ and notifies the
    taey-ed tmux session with a short prompt. The receiving Spark Claude has
    the codebase loaded (CLAUDE.md, BT handler reference) so we send pointers,
    not embedded documentation.
    """
    _require_worker_mode()
    CONSULT_DIR.mkdir(parents=True, exist_ok=True)

    # ONE AT A TIME: if any consultation is pending AND not yet responded AND
    # not stale (TTL), return it. abandoned status is non-blocking.
    for _p in CONSULT_DIR.iterdir():
        if not _p.is_dir() or not _p.name.startswith("consult_"):
            continue
        if (_p / "response.json").exists():
            continue
        _mf = _p / "metadata.json"
        if _mf.exists():
            try:
                _m = json.loads(_mf.read_text())
                if _pending_consult_is_blocking(_m, _p):
                    existing_id = _m.get("consultation_id", "")
                    return {
                        "consultation_id": existing_id,
                        "status": "existing",
                        "message": f"Waiting on existing consultation {existing_id}",
                    }
            except Exception:
                continue

    consultation_id = f"consult_{int(time.time())}_{uuid.uuid4().hex[:8]}"
    consult_path = CONSULT_DIR / consultation_id
    consult_path.mkdir(parents=True, exist_ok=True)

    if screenshot_b64:
        if is_valid_png_b64(screenshot_b64):
            (consult_path / "screenshot.png").write_bytes(base64.b64decode(screenshot_b64))
        else:
            logger.error(
                f"Rejected screenshot_b64 for minimal consult {consultation_id}: "
                f"not a valid PNG (len={len(screenshot_b64)}). "
                f"No screenshot.png written."
            )

    atomic_write_json(consult_path / "tree.json", tree)

    # Normalize KB chunks: accept Pydantic models or plain dicts
    kb_payload = []
    for ch in (relevant_kb_chunks or []):
        if hasattr(ch, "model_dump"):
            kb_payload.append(ch.model_dump())
        elif isinstance(ch, dict):
            kb_payload.append(ch)

    coordination_screen_hash = _coordination_screen_hash(tree)

    metadata = {
        "consultation_id": consultation_id,
        "platform": platform,
        "screen_type_hint": screen_type,
        "screen_hash": compute_tree_hash(tree),
        "coordination_screen_hash": coordination_screen_hash,
        "context": {
            "screen_type_hint": screen_type,
            "user_guidance": user_guidance or "",
        },
        "timestamp": datetime.now().isoformat(),
        "status": "pending",
        "escalation_level": "claude_primary",
        "spark_attempts": 0,
        # KB chunks retrieved by the Mac from local DeepTutor KB. May be empty.
        "relevant_kb_chunks": kb_payload,
    }
    atomic_write_json(consult_path / "metadata.json", metadata)
    _mirror_open_consult(metadata, consult_path, "request_minimal_consultation")

    set_consultation_state(consultation_id, ConsultationState(
        consultation_id=consultation_id,
        screen_hash=compute_tree_hash(tree),
        platform=platform,
    ))

    guidance_block = f"\nUser guidance / failure context:\n{user_guidance}\n" if user_guidance else ""
    notification = (
        f"CLAUDE-PRIMARY CONSULTATION {consultation_id}\n"
        f"Platform: {platform}\n"
        f"Screen-type hint: {screen_type}\n"
        f"Files: {consult_path}/screenshot.png, {consult_path}/tree.json\n"
        f"Knowledge: spark/platforms/{platform}/knowledge.json — "
        f"check the matching `subtype.operational_notes` for prior lessons "
        f"(exact roles, casing quirks, BT templates that worked) before building.\n"
        f"{guidance_block}"
        f"Look at the screenshot, read the tree, build a behavior tree to advance "
        f"this screen, and write {consult_path}/response.json with shape:\n"
        f'  {{"tree": <BT>, "screen_type": "<TYPE>", '
        f'"expected_next": [], "extract": null}}\n'
        f"BT format and handler list are in CLAUDE.md. Never click Skip or Up next.\n"
        f"After successful resolution of a previously-unsolved widget, append a new "
        f"entry under the matching screen type's learned observations in knowledge.json "
        f"so the next consultation reuses your insight."
    )

    logger.info(
        f"Minimal consultation created: {consultation_id} "
        f"(worker mode — no tmux notify)"
    )

    _cleanup_old_consultations(keep=2)

    return {
        "consultation_id": consultation_id,
        "status": "pending",
        "message": "Worker picks up via poll",
        "path": str(consult_path),
    }


def check_consultation(consultation_id: str) -> dict:
    """
    Check if consultation response is available.

    Returns:
        {"status": "pending|complete|escalated|user_required", ...}
    """
    consult_path = CONSULT_DIR / consultation_id

    if not consult_path.exists():
        return {
            "status": "not_found",
            "error": f"Consultation {consultation_id} not found",
        }

    # Check for response.json
    response_file = consult_path / "response.json"
    if response_file.exists():
        try:
            response = json.loads(response_file.read_text())
            return {"status": "complete", **response}
        except Exception as e:
            return {"status": "error", "error": str(e)}

    # Check metadata for escalation status
    metadata_file = consult_path / "metadata.json"
    if metadata_file.exists():
        try:
            metadata = json.loads(metadata_file.read_text())
            return {
                "status": metadata.get("status", "pending"),
                "escalation_level": metadata.get("escalation_level", "spark_claude"),
                "spark_attempts": metadata.get("spark_attempts", 0),
                "message": f"Awaiting {metadata.get('escalation_level', 'spark_claude')} review...",
            }
        except Exception:
            pass

    return {"status": "pending", "message": "Awaiting Spark Claude review..."}


def get_pending_consultations() -> List[dict]:
    """Get all pending consultation requests."""
    pending = []

    if not CONSULT_DIR.exists():
        return pending

    for path in CONSULT_DIR.iterdir():
        if path.is_dir() and path.name.startswith("consult_"):
            # Skip if already has response
            if (path / "response.json").exists():
                continue

            metadata_file = path / "metadata.json"
            if metadata_file.exists():
                try:
                    metadata = json.loads(metadata_file.read_text())
                    metadata["path"] = str(path)
                    pending.append(metadata)
                except Exception as e:
                    logger.error(f"Error reading metadata for {path.name}: {e}")

    return sorted(pending, key=lambda x: x.get("timestamp", ""))


def _cleanup_old_consultations(keep: int = 2):
    """Remove old completed consultations, keeping the most recent `keep` per platform."""
    import shutil

    if not CONSULT_DIR.exists():
        return

    # Collect completed consultations with timestamps
    completed = []
    for path in CONSULT_DIR.iterdir():
        if not path.is_dir() or not path.name.startswith("consult_"):
            continue
        if not (path / "response.json").exists():
            continue  # Keep pending consultations
        meta_file = path / "metadata.json"
        ts = ""
        if meta_file.exists():
            try:
                ts = json.loads(meta_file.read_text()).get("timestamp", "")
            except Exception:
                pass
        completed.append((ts, path))

    # Sort newest first, remove everything beyond `keep`
    completed.sort(key=lambda x: x[0], reverse=True)
    for _, path in completed[keep:]:
        try:
            shutil.rmtree(path)
            logger.info(f"Cleaned up old consultation: {path.name}")
        except Exception as e:
            logger.warning(f"Failed to clean up {path.name}: {e}")
