"""
Consultation request handling.

Creates and checks consultation requests for unknown screens.
Includes knowledge gate: no knowledge.json = research-first notification.

V8 change: Uses prompt_codex.compile_prompt() for comprehensive prompts.
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

logger = logging.getLogger(__name__)

CONSULT_DIR = Path("/tmp/taey-ed-consult")

# ONE consultation at a time. Period.
# If one is pending, every code path returns it instead of creating another.


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

    # ONE AT A TIME: If any consultation is pending, return it.
    for _p in CONSULT_DIR.iterdir():
        if not _p.is_dir() or not _p.name.startswith("consult_"):
            continue
        _mf = _p / "metadata.json"
        if _mf.exists():
            try:
                _m = json.loads(_mf.read_text())
                if _m.get("status") == "pending":
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

    # Save screenshot
    if screenshot_b64:
        try:
            screenshot_bytes = base64.b64decode(screenshot_b64)
            (consult_path / "screenshot.png").write_bytes(screenshot_bytes)
        except Exception as e:
            logger.error(f"Failed to save screenshot: {e}")

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

        if spark_attempts >= 2:
            escalation_level = "perplexity"
        logger.info(
            f"Reconsultation: {spark_attempts} previous attempts "
            f"→ escalation_level={escalation_level}"
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

    # Hard cap: after Perplexity attempt, next failure = user escalation
    if escalation_level == "perplexity" and is_reconsultation and spark_attempts >= 3:
        escalation_level = "user"
    if spark_attempts >= 3:
        escalation_level = "user"

    # User escalation — return directive instead of creating another consultation
    if escalation_level == "user":
        logger.warning(
            f"MAX_CONSULTATION_STEPS reached ({spark_attempts} attempts). "
            f"Escalating to user."
        )
        metadata["escalation_level"] = "user"
        metadata["status"] = "user_required"
        atomic_write_json(consult_path / "metadata.json", metadata)

        notify_spark_claude(
            f"ESCALATION TO USER: Consultation {consultation_id} for {platform} "
            f"has exhausted all automatic resolution ({spark_attempts} attempts). "
            f"User input required."
        )
        return {
            "consultation_id": consultation_id,
            "status": "user_required",
            "message": f"Exhausted {spark_attempts} attempts. User input needed.",
            "path": str(consult_path),
        }

    # V8: Comprehensive self-contained prompt via prompt_codex
    from .prompt_codex import compile_prompt

    consultation_context = {
        "escalation_level": escalation_level,
        "course_id": context.get("course_id", "unknown"),
        "failure_reason": context.get("failure_reason", ""),
        "previous_screen_type": context.get("previous_screen", ""),
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
    notify_spark_claude(notification)

    logger.info(f"Consultation created: {consultation_id} at {consult_path}")

    # Rolling cleanup: keep only 2 most recent completed consultations
    _cleanup_old_consultations(keep=2)

    return {
        "consultation_id": consultation_id,
        "status": "pending",
        "message": "Spark Claude notified via tmux",
        "path": str(consult_path),
    }


def request_minimal_consultation(
    platform: str,
    tree: dict,
    screenshot_b64: str,
    screen_type: str = "UNKNOWN",
    user_guidance: str | None = None,
) -> dict:
    """
    Bypass-Gemini consultation for Claude-primary platforms.

    Saves tree + screenshot to /tmp/taey-ed-consult/{id}/ and notifies the
    taey-ed tmux session with a short prompt. The receiving Spark Claude has
    the codebase loaded (CLAUDE.md, BT handler reference) so we send pointers,
    not embedded documentation.
    """
    CONSULT_DIR.mkdir(parents=True, exist_ok=True)

    # ONE AT A TIME: if any consultation is pending AND not yet responded,
    # return it. A consultation with response.json on disk is effectively
    # complete even if metadata.status was never flipped (Spark Claude
    # writes the response file directly without going through the API).
    for _p in CONSULT_DIR.iterdir():
        if not _p.is_dir() or not _p.name.startswith("consult_"):
            continue
        if (_p / "response.json").exists():
            continue
        _mf = _p / "metadata.json"
        if _mf.exists():
            try:
                _m = json.loads(_mf.read_text())
                if _m.get("status") == "pending":
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
        try:
            (consult_path / "screenshot.png").write_bytes(base64.b64decode(screenshot_b64))
        except Exception as e:
            logger.error(f"Failed to save screenshot: {e}")

    atomic_write_json(consult_path / "tree.json", tree)

    metadata = {
        "consultation_id": consultation_id,
        "platform": platform,
        "screen_hash": compute_tree_hash(tree),
        "context": {
            "screen_type_hint": screen_type,
            "user_guidance": user_guidance or "",
        },
        "timestamp": datetime.now().isoformat(),
        "status": "pending",
        "escalation_level": "claude_primary",
        "spark_attempts": 0,
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
        f"Knowledge: spark/platforms/{platform}/knowledge.json\n"
        f"{guidance_block}"
        f"Look at the screenshot, read the tree, build a behavior tree to advance "
        f"this screen, and write {consult_path}/response.json with shape:\n"
        f'  {{"tree": <BT>, "screen_type": "<TYPE>", '
        f'"expected_next": [], "extract": null}}\n'
        f"BT format and handler list are in CLAUDE.md. Never click Skip or Up next."
    )
    notify_spark_claude(notification)

    logger.info(f"Minimal consultation created: {consultation_id} at {consult_path}")
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
