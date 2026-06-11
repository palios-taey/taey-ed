"""
Consultation request handling.

Creates and checks consultation requests for unknown screens.
Includes knowledge gate: no knowledge.json = research-first notification.

Uses prompt_codex.compile_prompt() for comprehensive prompts.
V21 change: Gate checks knowledge.json instead of RESEARCH.md.
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
    metadata = {
        "consultation_id": consultation_id,
        "platform": platform,
        "screen_hash": compute_tree_hash(tree),
        "context": context,
        "timestamp": datetime.now().isoformat(),
        "status": "pending",
        "escalation_level": escalation_level,
        "spark_attempts": spark_attempts,
    }
    atomic_write_json(consult_path / "metadata.json", metadata)

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

    # Build notification preambles
    research_preamble = ""
    if needs_research:
        research_preamble = (
            f"RESEARCH REQUIRED FIRST: No knowledge.json exists for platform '{platform}'.\n"
            f"You MUST use Perplexity Deep Research via taey's hands MCP tools BEFORE mapping any screens.\n"
            f"DO NOT use WebSearch, WebFetch, or any other substitute. Perplexity is the ONLY acceptable method.\n"
            f"DO NOT delegate this to a subagent — subagents cannot use MCP tools.\n"
            f"Use the Perplexity research to create a knowledge.json file at:\n"
            f"  spark/platforms/{platform}/knowledge.json\n"
            f"The knowledge.json must follow the schema: platform, schema_version, global (timing, never_click, platform_quirks),\n"
            f"screen_types (each with handlers_needed, question_types, submit_button, extraction hints),\n"
            f"and accessibility_tree_guide. See existing knowledge.json files for reference.\n"
            f"ONLY AFTER saving knowledge.json, proceed to map the screen below.\n\n"
        )

    escalation_preamble = ""
    if escalation_level == "perplexity":
        escalation_preamble = (
            f"ESCALATION TIER 2 — PERPLEXITY DEEP RESEARCH REQUIRED.\n"
            f"Previous {spark_attempts} Spark Claude fixes FAILED for this screen.\n"
            f"You MUST complete ALL steps below in order BEFORE creating a consultation response.\n\n"
            f"=== MECHANICAL RUNBOOK (follow exactly) ===\n\n"
            f"STEP 1: Build combined context file\n"
            f"  Read the consultation files at {consult_path}/\n"
            f"  Combine: tree.json, screenshot.png, bt_debug.log, metadata.json\n\n"
            f"STEP 2: Prepare Perplexity session\n"
            f"  Call MCP tool: taey_inspect(platform='perplexity')\n"
            f"  Call MCP tool: taey_set_map(platform='perplexity', controls={{...}})\n\n"
            f"STEP 3: Attach context and enable Deep Research\n"
            f"  Call MCP tool: taey_attach(platform='perplexity', file_path=<context_file>)\n\n"
            f"STEP 4: Send research query about this screen type and failure\n"
            f"  Call MCP tool: taey_send_message(platform='perplexity', message=<query>)\n\n"
            f"STEP 5: Wait for response (Deep Research takes 2-5 minutes)\n"
            f"  Monitor daemon spawns automatically. Wait for the notification.\n\n"
            f"STEP 6: Extract research and create/update knowledge.json\n"
            f"  Call MCP tool: taey_quick_extract(platform='perplexity', complete=True)\n"
            f"  Parse the research into structured knowledge.json format.\n"
            f"  Save to: spark/platforms/{platform}/knowledge.json\n\n"
            f"STEP 7: NOW create consultation response using the research findings\n"
            f"  Create a FUNDAMENTALLY DIFFERENT tree based on the research.\n"
            f"  Respond to the consultation as normal.\n\n"
            f"=== END RUNBOOK ===\n\n"
        )

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
        # Use skeleton_hash to key the diagnosis state dir — must match the
        # hash used by routes/next_action.py::_escalate_to_claude_diagnosing
        # helper so both paths reference the SAME state dir and flag files.
        try:
            from spark.tasks.skeleton import (
                extract_skeleton as _ext_sk_cr, skeleton_hash as _skel_hash_cr,
            )
            screen_hash = _skel_hash_cr(_ext_sk_cr(tree))
        except Exception:
            screen_hash = compute_tree_hash(tree)
        diag_state_dir = Path("/tmp/taey-ed-claude-diagnosing") / f"{platform}_{screen_hash[:16]}"
        diag_state_dir.mkdir(parents=True, exist_ok=True)
        diagnosing_flag = diag_state_dir / "diagnosing.flag"
        done_flag = diag_state_dir / "diagnosis_done.flag"
        gave_up_flag = diag_state_dir / "gave_up.flag"
        retry_count_path = diag_state_dir / "retries.txt"

        # Real user escalation: claude explicitly gave up
        if gave_up_flag.exists():
            logger.warning(
                f"Claude explicitly gave up on screen_hash={screen_hash[:16]}. "
                f"Escalating to user."
            )
            metadata["escalation_level"] = "user"
            metadata["status"] = "user_required"
            atomic_write_json(consult_path / "metadata.json", metadata)
            notify_spark_claude(
                f"ESCALATION TO USER: Consultation {consultation_id} for {platform} "
                f"— claude gave up after diagnosis attempts. User input required."
            )
            return {
                "consultation_id": consultation_id,
                "status": "user_required",
                "message": f"Exhausted {spark_attempts} attempts + claude diagnosis. User input needed.",
                "path": str(consult_path),
            }

        # Diagnosis retry count: number of COMPLETED diagnosis cycles for this
        # (platform, screen_hash). 0 = first ever, 1 = one cycle finished, etc.
        retry_count = 0
        if retry_count_path.exists():
            try:
                retry_count = int(retry_count_path.read_text().strip())
            except (ValueError, OSError):
                retry_count = 0

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
            gave_up_flag.touch()
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
            notify_spark_claude(
                f"TERMINAL ESCALATION — {platform} screen_hash {screen_hash[:16]} "
                f"marked unsolvable after 6-tier exhaustion. "
                f"Logged to {UNSOLVED_LOG}.",
                notify_type="defect",
            )
            return {
                "consultation_id": consultation_id,
                "status": "user_required",
                "message": f"Escalation ladder exhausted ({retry_count} cycles). Marked unsolvable.",
                "path": str(consult_path),
            }

        # Diagnosis just completed: increment retry_count, reset escalation,
        # fall through to a fresh worker consult that picks up updated
        # knowledge.json via hot-reload.
        if done_flag.exists():
            logger.info(
                f"Diagnosis cycle complete for screen_hash={screen_hash[:16]}. "
                f"retry_count {retry_count} -> {retry_count + 1}. "
                f"Next tier: {tier_for_attempt(retry_count + 1)}."
            )
            retry_count_path.write_text(str(retry_count + 1))
            done_flag.unlink()
            diagnosing_flag.unlink(missing_ok=True)
            spark_attempts = 0
            escalation_level = "spark_claude"
            metadata["spark_attempts"] = 0
            metadata["escalation_level"] = "spark_claude"
            metadata["claude_diagnosis_cycles"] = retry_count + 1
            atomic_write_json(consult_path / "metadata.json", metadata)
            # Fall through to normal worker dispatch below

        else:
            # First entry to diagnosing for this cycle, or already-pending.
            # Notify once per cycle; build the rich-context escalation packet
            # so the recipient (claude-primary) and any Tier 2/3 dispatch has
            # everything it needs in one document.
            if not diagnosing_flag.exists():
                diagnosing_flag.touch()
                screen_type_hint = metadata.get("screen_type_hint", "UNKNOWN")

                # Build the packet. Operational notes rendering deferred to
                # caller knowledge if available; here we pass empty string and
                # let the worker prompt include them at BT-gen time.
                try:
                    from .knowledge_loader import (
                        load_knowledge as _lk, get_operational_notes_for_screen,
                    )
                    knowledge = _lk(platform)
                    notes_md = get_operational_notes_for_screen(knowledge, screen_type_hint)
                except Exception as e:
                    logger.warning(f"escalation packet: ops-notes load failed: {e}")
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
                )
                if dispatch_body:
                    notify_fleet("taeys-hands", dispatch_body, notify_type="task")
                logger.warning(
                    f"Escalation triggered for {consultation_id} "
                    f"({platform}, {screen_type_hint}, hash={screen_hash[:16]}, "
                    f"tier={tier}, retry_count={retry_count}, "
                    f"auto_dispatched={'yes' if dispatch_body else 'n/a'})"
                )
            metadata["status"] = "claude_diagnosing"
            metadata["escalation_level"] = f"diagnosing_{tier}"
            atomic_write_json(consult_path / "metadata.json", metadata)
            return {
                "consultation_id": consultation_id,
                "status": "claude_diagnosing",
                "message": f"Escalation tier={tier} active — Mac will retry automatically.",
                "path": str(consult_path),
            }

    # Comprehensive self-contained prompt via prompt_codex
    from .prompt_codex import compile_prompt

    # Pass screen_type to prompt builder so it can inject subtype-specific
    # operational_notes from knowledge.json. Falls back to "UNKNOWN" only when
    # we genuinely have no hint; the prompt builder treats UNKNOWN as "include
    # all subtype notes since we don't know which applies yet" via the
    # master-category fallback in get_operational_notes_for_screen.
    screen_type_for_prompt = (
        context.get("screen_type")
        or metadata.get("screen_type_hint")
        or "UNKNOWN"
    )
    consultation_context = {
        "escalation_level": escalation_level,
        "course_id": context.get("course_id", "unknown"),
        "failure_reason": context.get("failure_reason", ""),
        "previous_screen_type": context.get("previous_screen", ""),
        "screen_type": screen_type_for_prompt,
    }

    consultation_details = compile_prompt(
        tree=tree,
        platform=platform,
        consultation_id=consultation_id,
        context=consultation_context,
        spark_attempts=spark_attempts,
        is_reconsultation=is_reconsultation,
    )

    # When Perplexity MCP tools are required (research gate or tier 2 escalation),
    # handle DIRECTLY — do NOT delegate to a subagent (they can't use MCP tools).
    requires_mcp = needs_research or escalation_level == "perplexity"

    if requires_mcp:
        notification = (
            f"HANDLE DIRECTLY — DO NOT delegate to a subagent (MCP tools required).\n\n"
            f"{escalation_preamble}{research_preamble}"
            f"{consultation_details}"
        )
    else:
        notification = (
            f"Use the Task tool NOW to launch an agent (subagent_type=general-purpose) "
            f"with this prompt:\n\n"
            f"\"{consultation_details}\""
        )

    # When the headless worker is enabled, skip the tmux notify. The worker
    # polls /tmp/taey-ed-consult/ for pending consultations and processes them
    # via claude --print subprocess. Per LAUNCH_PLAN.md Phase 2 — replaces the
    # tmux-to-interactive-Claude path for production scale.
    from spark.worker.consultation_worker import use_worker_enabled
    if use_worker_enabled():
        logger.info(
            f"Consultation created: {consultation_id} (worker mode — no tmux notify)"
        )
    else:
        notify_spark_claude(notification)
        logger.info(f"Consultation created: {consultation_id} at {consult_path}")

    # Rolling cleanup: keep only 2 most recent completed consultations
    _cleanup_old_consultations(keep=2)

    return {
        "consultation_id": consultation_id,
        "status": "pending",
        "message": (
            "Worker picks up via poll" if use_worker_enabled()
            else "Spark Claude notified via tmux"
        ),
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

    metadata = {
        "consultation_id": consultation_id,
        "platform": platform,
        "screen_type_hint": screen_type,
        "screen_hash": compute_tree_hash(tree),
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
        f"entry under the matching subtype's `operational_notes` in knowledge.json "
        f"so the next consultation reuses your insight (use record_operational_note "
        f"helper in spark/tasks/knowledge_loader.py if writing programmatically)."
    )

    # When the headless worker is enabled, skip tmux notify.
    from spark.worker.consultation_worker import use_worker_enabled
    if use_worker_enabled():
        logger.info(
            f"Minimal consultation created: {consultation_id} "
            f"(worker mode — no tmux notify)"
        )
    else:
        notify_spark_claude(notification)
        logger.info(
            f"Minimal consultation created: {consultation_id} at {consult_path}"
        )

    _cleanup_old_consultations(keep=2)

    return {
        "consultation_id": consultation_id,
        "status": "pending",
        "message": "Spark Claude notified (minimal prompt)",
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
