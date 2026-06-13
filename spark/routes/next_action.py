"""
/next_action — V21 Directive Model.

Mac sends state, Spark returns ONE directive. All intelligence server-side.
Mac is a dumb executor; Spark makes all decisions.

Flow:
  1. Active consultation? → wait or execute completed BT
  2. Validate previous action → stuck detection, wrong-answer detection
  3. BT failure recovery → retry with context, delete bad hash mappings
  4. V21 Exact hash lookup → skeleton hash → variant → stored BT (free, ~0ms)
  5. No hash match → knowledge gate → Flash classify ($0.002) → variant BT cache → Pro ($0.09)

Screen types (6 universal + 1 escalation, IMS Caliper validated):
  NAVIGATION, VIDEO, ARTICLE, EXERCISE, TRANSITION, UNKNOWN

V21 (2026-02-28):
  - Replaced Jaccard signature matching with skeleton hash + Flash classification
  - Skeleton scoped to AXWebArea (excludes browser chrome)
  - Variant BT cache: BTs stored per variant, not per screen instance
  - Non-deterministic variants (EXERCISE_*) always get fresh Pro BT
  - Data files under TAEY_ED_DATA_DIR (see spark/tasks/paths.py)
"""

import json
import logging
import time
import uuid
from pathlib import Path

from fastapi import APIRouter

from spark.models import NextActionRequest, ClientState
from spark.tasks.handle_consultation import (
    request_consultation,
    check_consultation,
)
from spark.tasks.chat_store import (
    store_message,
    build_status,
    build_question,
    build_user_message,
)

logger = logging.getLogger(__name__)

router = APIRouter()

def _escalate_to_claude_diagnosing(
    platform: str,
    tree: dict | None,
    consultation_id: str,
    reason: str,
    screen_type_hint: str = "UNKNOWN",
    bt_debug_tail: str = "",
    failed_bt: dict | None = None,
    screenshot_b64: str | None = None,
) -> dict:
    """Route any failure through the Mira-side claude diagnosis loop.

    Per Jesse's directive (2026-05-18): the Mac app + API do not escalate to
    user. Everything that isn't perfectly handled goes to claude-on-Mira.
    State is keyed by (platform, skeleton_hash) so reconsults across multiple
    consult_ids see the same diagnosis state. Pings claude once per stuck-screen
    cycle; returns a wait directive so Mac keeps polling without surfacing a
    dialog. Only escalates to user_input_needed when claude EXPLICITLY touches
    the gave_up.flag in the state dir — Mac/Spark never make that decision.

    Persistence (2026-05-19 bug fix): tree and screenshot_b64 are written to
    the diag_dir as tree.json / screenshot.png so the escalation packet
    builder can read them. Required for the new Step 4.5 → claude-primary
    escalation path which has no consult_id (and therefore no consult dir
    with these artifacts).
    """
    try:
        from spark.tasks.skeleton import (
            extract_skeleton as _ext_sk, skeleton_hash as _skel_hash,
        )
        _screen_hash = _skel_hash(_ext_sk(tree)) if tree else "unknown"
    except Exception:
        _screen_hash = "unknown"

    diag_dir = Path("/tmp/taey-ed-claude-diagnosing") / f"{platform}_{_screen_hash[:16]}"
    diag_dir.mkdir(parents=True, exist_ok=True)
    diagnosing = diag_dir / "diagnosing.flag"
    done = diag_dir / "diagnosis_done.flag"
    gave_up = diag_dir / "gave_up.flag"
    retry_p = diag_dir / "retries.txt"

    # Persist tree + screenshot to diag_dir — ALWAYS OVERWRITE with the
    # current capture. The old write-once "idempotency" served STALE state:
    # on collision pages the question changes under the same hash, and on
    # 2026-06-11 claude-primary spent an hour mis-diagnosing a dropdown
    # question as a checkbox one because every escalation packet carried the
    # first-capture tree from a question answered long before ("Why are you
    # still not reviewing screenshots?"). The packet must show NOW.
    if tree:
        try:
            (diag_dir / "tree.json").write_text(json.dumps(tree, indent=2))
        except Exception:
            logger.exception("escalate: failed to write tree.json")
    if screenshot_b64:
        try:
            import base64
            (diag_dir / "screenshot.png").write_bytes(base64.b64decode(screenshot_b64))
        except Exception:
            logger.exception("escalate: failed to write screenshot.png")

    # Only path to user: claude explicitly gave up by touching gave_up.flag.
    # Mac/Spark cannot create this flag — only the Mira-side claude session can.
    if gave_up.exists():
        from spark.tasks.classify_screen import _describe_screen
        # Terminal user-assist (INTENDED_FLOW §D terminal; Jesse 2026-06-11):
        # when the system gives up on a screen, the user is notified WITH the
        # correct answer / exact steps to do it themselves. claude-primary
        # writes user_instructions.txt into the state dir alongside
        # gave_up.flag; the dialog carries those instructions verbatim.
        user_instr = ""
        instr_p = diag_dir / "user_instructions.txt"
        if instr_p.exists():
            try:
                user_instr = instr_p.read_text().strip()
            except OSError:
                pass
        question_text = user_instr or f"Claude diagnosis exhausted: {reason}"
        return _with_chat({
            "directive": "user_input_needed",
            "directive_id": _make_directive_id(),
            "reason": question_text,
            "screen_type": screen_type_hint,
            "screen_description": _describe_screen(tree) if tree else "",
            "consultation_id": consultation_id,
        }, platform, [
            build_status("This one needs your hands — exact steps in the dialog"),
            build_question(question_text),
        ])

    retries = 0
    if retry_p.exists():
        try:
            retries = int(retry_p.read_text().strip())
        except (ValueError, OSError):
            retries = 0

    # Claude finished diagnosing (knowledge.json updated). Clear flags + abandon
    # the stale consult so Mac's next /next_action runs the FRESH pipeline,
    # which spawns a new worker call using the updated knowledge.json.
    if done.exists():
        retries += 1
        retry_p.write_text(str(retries))
        done.unlink()
        diagnosing.unlink(missing_ok=True)
        # Abandon the stale consult so its metadata.status flips off
        # "complete" — Mac's next request will find no active consult
        # and trigger fresh pipeline.
        if consultation_id:
            try:
                _stale_path = Path("/tmp/taey-ed-consult") / consultation_id
                _meta_p = _stale_path / "metadata.json"
                if _meta_p.exists():
                    _m = json.loads(_meta_p.read_text())
                    _m["status"] = "abandoned"
                    _m["abandoned_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
                    _m["abandoned_reason"] = "claude_diagnosis_complete_retry"
                    _meta_p.write_text(json.dumps(_m, indent=2))
                # Remove response.json so Mac status poll won't see it as complete
                _resp = _stale_path / "response.json"
                if _resp.exists():
                    _resp.unlink()
            except Exception:
                logger.exception("failed to abandon stale consult on diagnosis_done")
        # A completed diagnosis means knowledge changed — stale variant
        # failure counts predate the fix and must not preempt the retry
        # (observed 14:48: a healthy phase-1 scroll cycle got loop-guarded
        # by attempt-1's leftover count and climbed to a spurious Tier-2).
        _clear_variant_failures(platform)
        logger.info(
            f"Claude diagnosis complete for {platform}_{_screen_hash[:16]} — "
            f"abandoned stale consult {consultation_id}, retry cycle {retries}, "
            f"variant failure counters cleared"
        )
        return _with_chat({
            "directive": "wait",
            "directive_id": _make_directive_id(),
            "seconds": 3.0,
            "reason": "claude_diagnosis_complete",
            "message": "Claude diagnosis complete — retrying with updated knowledge.",
            "consultation_id": "",
        }, platform, [
            build_status("Claude diagnosis complete — retrying with updated knowledge"),
        ])

    # Tier-aware escalation per Jesse 2026-05-19 ladder
    # (2 me → 1 Perplexity → 1 Family → terminal). The notify body and packet
    # both come from spark.tasks.escalation; this helper just figures out the
    # tier from `retries`, builds the packet, and dispatches.
    from spark.tasks.escalation import (
        tier_for_attempt, build_packet, notify_body_for_tier, UNSOLVED_LOG,
    )
    tier = tier_for_attempt(retries)

    # Persist Mac log in the diag state dir BEFORE building the packet so the
    # packet's attempt-history section can pick it up.
    if bt_debug_tail and bt_debug_tail.strip():
        try:
            (diag_dir / "last_bt_debug.log").write_text(bt_debug_tail)
        except Exception:
            pass

    # Terminal tier: auto-mark unsolvable, log, return user_input_needed.
    if tier == "terminal":
        try:
            gave_up.touch()
            UNSOLVED_LOG.parent.mkdir(parents=True, exist_ok=True)
            with UNSOLVED_LOG.open("a") as fh:
                fh.write(
                    f"\n## {time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())} "
                    f"— {platform} {_screen_hash[:16]}\n"
                    f"- escalation_path: live_bt_failure\n"
                    f"- consultation_id: {consultation_id}\n"
                    f"- attempts_exhausted: {retries}\n"
                    f"- state_dir: {diag_dir}\n"
                    f"- failure_reason: {reason}\n"
                )
        except Exception:
            logger.exception("UNSOLVED.md append failed")
        try:
            from spark.tasks.notify_tmux import notify_spark_claude as _notify
            _notify(
                f"TERMINAL ESCALATION — {platform} screen_hash {_screen_hash[:16]} "
                f"marked unsolvable after 4-tier exhaustion. "
                f"Logged to {UNSOLVED_LOG}.",
                notify_type="defect",
            )
        except Exception:
            logger.exception("notify_spark_claude failed on terminal")
        from spark.tasks.classify_screen import _describe_screen
        return _with_chat({
            "directive": "user_input_needed",
            "directive_id": _make_directive_id(),
            "reason": f"Escalation ladder exhausted: {reason}",
            "screen_type": screen_type_hint,
            "screen_description": _describe_screen(tree) if tree else "",
            "consultation_id": consultation_id,
        }, platform, [
            build_status(f"Escalation ladder exhausted on {screen_type_hint}"),
            build_question(f"Cannot proceed: {reason}"),
        ])

    # Non-terminal: build rich-context packet + tier-aware notify (once per
    # stuck-screen cycle until done/gave_up).
    if not diagnosing.exists():
        diagnosing.touch()
        consult_path = Path("/tmp/taey-ed-consult") / consultation_id if consultation_id else diag_dir
        # Compose Mac BT execution log + failed BT into an attempts.jsonl-like
        # entry inside the state dir so build_packet's attempt-history section
        # surfaces them in the packet.
        try:
            attempts_path = diag_dir / "attempts.jsonl"
            attempt_record = {
                "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "classification": screen_type_hint,
                "failure_mode": reason,
                "analysis": "",
                "bt": failed_bt or {},
                "mac_log_tail": (bt_debug_tail or "")[-4000:],
            }
            with attempts_path.open("a") as fh:
                fh.write(json.dumps(attempt_record) + "\n")
        except Exception:
            logger.exception("attempts.jsonl append failed (non-fatal)")

        knowledge = {}
        notes_md = ""

        try:
            packet_path = build_packet(
                platform=platform,
                screen_hash=_screen_hash,
                consult_path=consult_path,
                diag_state_dir=diag_dir,
                retry_count=retries,
                knowledge=knowledge,
                operational_notes_rendered=notes_md,
                screen_type_hint=screen_type_hint,
            )
        except Exception:
            logger.exception("build_packet failed; falling back to diag_dir path")
            packet_path = diag_dir / "(packet_build_failed)"

        body = notify_body_for_tier(
            tier=tier,
            packet_path=packet_path,
            platform=platform,
            screen_hash=_screen_hash,
            retry_count=retries,
            consult_path=consult_path,
            diag_state_dir=diag_dir,
        )
        try:
            from spark.tasks.notify_tmux import notify_spark_claude as _notify
            _notify(body, notify_type="escalation")
        except Exception:
            logger.exception("notify_spark_claude failed in escalate helper")

        # Auto-climb (INTENDED_FLOW §D): Tier 2/3 packets go to taeys-hands
        # DIRECTLY from the server. This emission site was MISSED when the
        # auto-dispatch shipped (only consultation_request.py was wired) —
        # both 2026-06-11 Tier-2 escalations claimed "AUTO-DISPATCHED" while
        # nothing was sent (Jesse caught it: "Taeys-Hands isn't getting
        # anything from you").
        dispatch_body = None
        try:
            from spark.tasks.escalation import dispatch_body_for_tier, notify_fleet
            dispatch_body = dispatch_body_for_tier(
                tier=tier,
                packet_path=packet_path,
                platform=platform,
                screen_hash=_screen_hash,
                retry_count=retries,
                bt_debug_tail=bt_debug_tail,
            )
            if dispatch_body:
                notify_fleet("taeys-hands", dispatch_body, notify_type="task")
        except Exception:
            logger.exception("taeys-hands auto-dispatch failed in escalate helper")
        logger.warning(
            f"Escalation triggered for {consultation_id} "
            f"({platform}, {screen_type_hint}, hash={_screen_hash[:16]}, "
            f"tier={tier}, retry_count={retries}, "
            f"auto_dispatched={'yes' if dispatch_body else 'n/a'}, reason={reason!r})"
        )

    return _with_chat({
        "directive": "wait",
        "directive_id": _make_directive_id(),
        "seconds": 30.0,
        "reason": "claude_diagnosing",
        "message": f"Claude diagnosing this screen — Mac will retry automatically. ({reason})",
        "consultation_id": consultation_id,
    }, platform, [
        build_status("Claude diagnosing this screen — will retry automatically"),
    ])


# ── Failure tracking (prevents infinite reclassify loops) ──
# Key: "{platform}:{variant}" → {"count": int, "last_failed": float}
# Reset when a DIFFERENT variant succeeds on the same platform.
_variant_failures: dict[str, dict] = {}
MAX_VARIANT_FAILURES = 1  # ONE SHOT: after 1 failure, escalate to claude (was 2 — Jesse 2026-05-18)


def _record_variant_failure(platform: str, variant: str):
    """Record that a variant's BT failed. Used to prevent reclassify loops."""
    key = f"{platform}:{variant}"
    entry = _variant_failures.get(key, {"count": 0})
    entry["count"] += 1
    entry["last_failed"] = time.time()
    _variant_failures[key] = entry
    logger.info(f"  Failure tracker: {variant} fail_count={entry['count']}")


def _check_variant_failed(platform: str, variant: str) -> bool:
    """Check if a variant has exceeded max failures (would loop if retried)."""
    key = f"{platform}:{variant}"
    entry = _variant_failures.get(key)
    if not entry:
        return False
    # Expire failures after 30 minutes (allow retry after cooldown)
    if time.time() - entry.get("last_failed", 0) > 1800:
        del _variant_failures[key]
        return False
    return entry["count"] >= MAX_VARIANT_FAILURES


def _clear_variant_failures(platform: str):
    """Clear failure tracking for a platform (on successful screen transition)."""
    to_clear = [k for k in _variant_failures if k.startswith(f"{platform}:")]
    for k in to_clear:
        del _variant_failures[k]


def _make_directive_id() -> str:
    return f"d-{uuid.uuid4().hex[:8]}"


def _bt_actions_from_tail(bt_debug_tail) -> list:
    """Parse the executed action names from the Mac's BT debug tail."""
    import re as _re
    if not bt_debug_tail:
        return []
    return [a for _, a in _re.findall(r"seq step (\d+)/\d+: (\w+)", str(bt_debug_tail))]


def _record_screen_failure(platform: str, skel_hash, bt_debug_tail, outcome: str, detail: str = ""):
    """Screen-session hook for failure outcomes (Jesse 2026-06-11)."""
    if not skel_hash:
        return
    try:
        from spark.tasks.screen_session import record_attempt
        record_attempt(platform, skel_hash,
                       bt_actions=_bt_actions_from_tail(bt_debug_tail),
                       outcome=outcome, detail=detail, author="machine")
    except Exception:
        logger.exception("screen_session failure-record failed (continuing)")


def _is_scroll_only_bt(bt_debug_tail) -> bool:
    """True if the just-executed BT consisted solely of scroll/wait actions
    (phase-1 of the two-phase drag pattern). Parsed from the Mac's BT debug
    tail; requires step 1 visible so a truncated tail of a longer mixed BT
    can never masquerade as scroll-only."""
    import re as _re
    if not bt_debug_tail:
        return False
    steps = _re.findall(r"seq step (\d+)/\d+: (\w+)", str(bt_debug_tail))
    if not steps or steps[0][0] != "1":
        return False
    return all(action in ("scroll", "wait") for _, action in steps)


def _get_extract_for_type(screen_type: str, tree: dict = None,
                          screenshot_b64: str = None, platform: str = None):
    """Build extract config for signatures missing stored extract.

    Uses dedicated Gemini call if screenshot available, otherwise returns None.
    """
    from spark.tasks.classify_screen import _should_extract, build_extract_config
    if not _should_extract(screen_type):
        return None
    if screenshot_b64 and tree and platform:
        return build_extract_config(tree, screenshot_b64, platform, screen_type)
    return None


from spark.tasks.paths import FINGERPRINT_LOG_DIR


def _log_fingerprint(platform: str, variant: str, skel_hash: str, fingerprint: dict):
    """Append fingerprint entry to JSONL log for V22 learning."""
    from datetime import datetime, timezone
    FINGERPRINT_LOG_DIR.mkdir(parents=True, exist_ok=True)
    log_path = FINGERPRINT_LOG_DIR / f"{platform}.jsonl"
    entry = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "variant": variant,
        "skeleton_hash": skel_hash,
        "fingerprint": fingerprint,
    }
    with open(log_path, "a") as f:
        f.write(json.dumps(entry) + "\n")
    logger.info(f"  Fingerprint logged: variant={variant} hash={skel_hash[:12]}")


def _with_chat(response: dict, platform: str, messages: list[dict]) -> dict:
    """Attach chat_messages to a response and persist them to Redis."""
    for msg in messages:
        store_message(platform, msg)
    response["chat_messages"] = messages
    return response



def _find_submit_in_bt(bt_def: dict) -> dict:
    """Walk BT to find submit/check button click."""
    if not isinstance(bt_def, dict):
        return {}

    if bt_def.get("type") == "action" and bt_def.get("action") == "find_and_click":
        params = bt_def.get("params", {})
        target = params.get("target", "")
        if target and isinstance(target, str) and not target.startswith("$"):
            role = params.get("role", "")
            if role in ("AXButton", "") and any(
                kw in target.lower() for kw in ["check", "submit", "next", "continue", "done"]
            ):
                return {
                    "text": target,
                    "role": role or "AXButton",
                    "strategy": params.get("strategy", "mouse_click"),
                }

    for child in bt_def.get("children", []):
        result = _find_submit_in_bt(child)
        if result:
            return result

    for key in ("do", "then", "else"):
        if key in bt_def:
            result = _find_submit_in_bt(bt_def[key])
            if result:
                return result

    return {}


def learn_from_bt_result(
    platform: str,
    screen_type: str,
    bt_result: dict,
    bt_definition: dict,
    skeleton_hash: str,
):
    """
    Extract observations from BT execution and save to learned data.
    Called after every execute_tree completion (success or interesting failure).
    Non-blocking: failures logged but don't stop pipeline.
    """
    try:
        from spark.tasks.knowledge_loader import save_learned_observation

        observation = {
            "observed_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "bt_success": bt_result.get("success", False),
            "skeleton_hash": skeleton_hash,
            "variant": bt_result.get("screen_type", screen_type),
            "details": {},
        }

        submit = _find_submit_in_bt(bt_definition)
        if submit:
            observation["details"]["submit_button"] = submit

        extract = bt_result.get("extract", {})
        if extract and isinstance(extract, dict):
            text_configs = extract.get("text", [])
            containers = [tc.get("parent_contains") for tc in text_configs if tc.get("parent_contains")]
            if containers:
                observation["details"]["content_containers"] = containers

        if not bt_result.get("success"):
            observation["failure_reason"] = bt_result.get("action", "unknown")
            if "tree_unchanged" in str(observation.get("failure_reason", "")):
                return

        save_learned_observation(platform, screen_type, observation)
    except Exception as e:
        logger.warning(f"Learning failed (non-blocking): {e}")


def _consultation_or_wait(consult_result: dict) -> dict:
    """Convert request_consultation result to a directive."""
    status = consult_result.get("status", "")
    if status == "user_required":
        return {
            "directive": "user_input_needed",
            "directive_id": _make_directive_id(),
            "reason": consult_result.get("message", "Automatic resolution exhausted"),
            "consultation_id": consult_result.get("consultation_id", ""),
        }
    if status == "claude_diagnosing":
        # Mira-side Claude is editing knowledge.json. Mac sleeps and re-sends
        # /next_action; once claude_diagnosis_done.flag is set, the next call
        # falls through to a fresh worker dispatch using the updated knowledge.
        return {
            "directive": "wait",
            "directive_id": _make_directive_id(),
            "seconds": 30.0,
            "reason": "claude_diagnosing",
            "message": consult_result.get(
                "message",
                "Claude diagnosing this screen variant — Mac will retry automatically.",
            ),
            "consultation_id": consult_result.get("consultation_id", ""),
        }
    return {
        "directive": "consulting",
        "directive_id": _make_directive_id(),
        "consultation_id": consult_result["consultation_id"],
        "poll_interval": 5.0,
    }


def _build_screen_directive(request, platform: str, tree: dict, screen_type: str, sig_hash: str,
                            user_guidance: str = None, course_id: str = "") -> dict:
    """
    Deterministic subtype YAMLs replay their stored fixed BT directly.
    Non-deterministic subtypes go through the worker consultation path.
    """
    from spark.tasks.classify_screen import canonicalize_screen_type

    canonical_screen_type = canonicalize_screen_type(platform, screen_type, tree)
    if canonical_screen_type != screen_type:
        logger.warning(
            "_build_screen_directive: canonicalized screen_type %s -> %s",
            screen_type,
            canonical_screen_type,
        )
    screen_type = canonical_screen_type

    if not request.screenshot_b64:
        return {
            "directive": "need_screenshot",
            "directive_id": _make_directive_id(),
            "reason": f"{screen_type.lower()}_consultation_needs_screenshot",
        }
    from spark.tasks.screen_type_assembler import (
        ScreenTypeAssemblerError,
        load_screen_artifact_metadata,
    )
    try:
        metadata = load_screen_artifact_metadata(platform, screen_type)
    except ScreenTypeAssemblerError:
        logger.exception("_build_screen_directive: failed to load screen metadata for %s", screen_type)
        metadata = None

    artifact = (metadata or {}).get("artifact") or {}
    artifact_screen_type = artifact.get("screen_type")
    artifact_kind = artifact.get("kind")

    if (
        screen_type != "UNKNOWN"
        and artifact_kind == "unknown_guide"
        and artifact_screen_type == "UNKNOWN"
    ):
        logger.error(
            "_build_screen_directive: unresolved non-UNKNOWN screen_type=%s for platform=%s; refusing worker fallback",
            screen_type,
            platform,
        )
        return _with_chat({
            "directive": "user_input_needed",
            "directive_id": _make_directive_id(),
            "reason": f"Subtype '{screen_type}' has no matching screen artifact; refusing uncached worker fallback.",
            "screen_type": screen_type,
        }, platform, [
            build_status(f"Subtype artifact missing for {screen_type}"),
            build_question(f"I recognized this as {screen_type}, but its screen artifact is missing. What should I do?"),
        ])

    if metadata and metadata.get("deterministic") and metadata.get("fixed_behavior_tree"):
        return _with_chat({
            "directive": "execute_tree",
            "directive_id": _make_directive_id(),
            "tree": metadata["fixed_behavior_tree"],
            "screen": screen_type,
            "extract": _get_extract_for_type(
                screen_type,
                tree,
                request.screenshot_b64,
                platform,
            ),
            "course_id": course_id,
            "lesson": "",
            "expected_next": [],
            "skeleton_hash": sig_hash,
        }, platform, [build_status(f"Using fixed {screen_type} automation")])

    if metadata and metadata.get("deterministic"):
        logger.error(
            "_build_screen_directive: deterministic screen_type=%s lacks fixed_behavior_tree; refusing worker fallback",
            screen_type,
        )
        return _with_chat({
            "directive": "user_input_needed",
            "directive_id": _make_directive_id(),
            "reason": f"Deterministic subtype '{screen_type}' has no fixed behavior tree.",
            "screen_type": screen_type,
        }, platform, [
            build_status(f"Deterministic automation missing for {screen_type}"),
            build_question(f"I recognized this as {screen_type}, but its fixed automation is missing. What should I do?"),
        ])

    consult_result = request_consultation(
        platform=platform,
        tree=tree,
        screenshot_b64=request.screenshot_b64,
        context={
            "screen_type": screen_type,
            "screen_type_hint": screen_type,
            "user_guidance": user_guidance or "",
            "course_id": course_id,
        },
    )
    return _consultation_or_wait(consult_result)


def _validate_last_action(platform: str, lr, current_tree: dict) -> dict:
    """
    Validate the result of the previous execute_tree directive.

    Checks:
    1. Did the tree hash change? (action had effect)
    2. Does the after_tree match a known screen?
    3. Same QUIZ/ASSESSMENT screen? (wrong answer detection)
    4. New screen in expected_next? (informational)
    """
    tree_changed = lr.tree_hash_before != lr.tree_hash_after

    if not tree_changed:
        return {
            "validated": False,
            "screen_transitioned": False,
            "wrong_answer": False,
            "reason": "same_screen",
        }

    # V21: Use skeleton hash to identify where we landed
    from spark.tasks.skeleton import extract_skeleton, skeleton_hash as _skel_hash
    from spark.tasks.variant_cache import lookup_by_hash

    after_tree_to_match = lr.after_tree or current_tree
    after_skel = extract_skeleton(after_tree_to_match)
    after_hash = _skel_hash(after_skel)
    hash_result = lookup_by_hash(platform, after_hash)
    new_screen = hash_result["variant"] if hash_result else None

    if not new_screen:
        logger.warning(
            f"Step 2: after_tree matches NO known variant "
            f"(hash changed {lr.tree_hash_before[:12]} → {lr.tree_hash_after[:12]}, "
            f"after_skel_hash={after_hash[:12]})"
        )

    # Wrong answer / not-advanced detection. Key on the SKELETON HASH, never
    # variant-name equality — unit-test pages share one skeleton across
    # question types, so the stored variant name can differ from lr.screen
    # while the screen is in fact unchanged (observed 2026-06-11: a ranking Q
    # reorder auto-validated as 'advanced' because EXERCISE_CHECKBOX !=
    # EXERCISE skipped this whole block, then poisoned credits downstream).
    wrong_answer = False
    not_advanced = False

    # VERDICT FIRST (2026-06-11, Jesse: wrong answers sailed through as
    # 'Validated' because the wrong-answer popover changes the tree, and
    # movement was being read as success): Khan prints the verdict — a
    # 'Not quite' heading in the feedback popover means WRONG, full stop,
    # regardless of any hash/screen movement.
    try:
        from spark.tasks.screen_type_util import get_master_category as _gmc
        if _gmc(lr.screen or "") == "EXERCISE":
            def _wrong_feedback(n) -> bool:
                if not isinstance(n, dict):
                    return False
                name = str(n.get("name") or n.get("title") or "")
                if (n.get("role") in ("AXHeading", "AXGroup")
                        and name.startswith("Not quite")):
                    return True
                return any(_wrong_feedback(c) for c in n.get("children") or [])
            if _wrong_feedback(after_tree_to_match or {}):
                logger.warning(
                    "Step 2: WRONG-ANSWER FEEDBACK in after-tree ('Not quite') — "
                    "verdict overrides movement."
                )
                wrong_answer = True
    except Exception:
        logger.exception("verdict scan failed (continuing with movement checks)")

    if not wrong_answer and new_screen:
        from spark.tasks.screen_type_util import get_master_category
        screen_master = get_master_category(new_screen) or ""
        directive_hash = lr.directive_skeleton_hash or ""
        if (screen_master == "EXERCISE" and after_hash and directive_hash
                and after_hash == directive_hash):
            if new_screen == lr.screen:
                # Full-tree disambiguation (2026-06-11, Q8): same screen after
                # a solve can mean WRONG ANSWER *or* CLICKS NEVER STAGED. The
                # capture now carries value/enabled — if no answer widget is
                # selected AND the submit button is disabled, nothing was
                # submitted: retry the same answer, don't treat as wrong.
                staged = None
                try:
                    submit_disabled = False
                    any_selected = False
                    def _scan(n):
                        nonlocal submit_disabled, any_selected
                        role = n.get("role") or ""
                        name = n.get("name") or ""
                        if role == "AXButton" and name == "Check" and n.get("enabled") is False:
                            submit_disabled = True
                        if role in ("AXCheckBox", "AXRadioButton") and name.startswith("(Choice"):
                            if n.get("value") in (1, "1", True):
                                any_selected = True
                        for c in n.get("children") or []:
                            _scan(c)
                    _scan(after_tree_to_match or {})
                    staged = any_selected or not submit_disabled
                except Exception:
                    staged = None
                if staged is False:
                    not_advanced = True
                    logger.warning(
                        "Step 2: solve produced NO staged selection (all choices "
                        "value=0, Check disabled) — clicks did not register. "
                        "NOT a wrong answer; rebuilding to retry."
                    )
                else:
                    wrong_answer = True
            else:
                # Same skeleton, different label — collision territory.
                # Without a content-fingerprint delta we cannot claim the
                # question advanced. Neutral: no validation, no map damage.
                not_advanced = True
                logger.info(
                    f"Step 2: after-skeleton equals directive skeleton "
                    f"({after_hash[:12]}) with label mismatch "
                    f"({lr.screen} → {new_screen}) — NOT validating "
                    f"(cannot prove the question advanced)."
                )
        elif (screen_master == "EXERCISE" and after_hash and directive_hash
                and after_hash != directive_hash):
            logger.info(
                f"Step 2: Exercise skeleton changed "
                f"({directive_hash[:12]} → {after_hash[:12]}) — "
                f"progress to next question."
            )

    # Expected_next check (informational)
    expected_next = lr.directive_expected_next or []
    expected_match = None
    if expected_next and new_screen:
        expected_match = new_screen in expected_next

    validated = tree_changed and not wrong_answer and not not_advanced

    return {
        "validated": validated,
        "screen_transitioned": tree_changed,
        "new_screen": new_screen,
        "wrong_answer": wrong_answer,
        "not_advanced": not_advanced,
        "expected_next_match": expected_match,
        "after_skeleton_hash": after_hash,
        "reason": ("validated" if validated else
                   ("wrong_answer" if wrong_answer else
                    ("not_advanced" if not_advanced else "validation_failed"))),
    }


@router.post("/session/reset")
def session_reset(platform: str = "khan_academy"):
    """Mac calls this when the user hits Stop on Run Continuous.
    Clears stale spark-side state so the next session starts fresh.
    Per Jesse 2026-05-20: 'we can't be out of sync. When user hits stop,
    it has to clear everything for that user.'

    Cleared:
      - /tmp/taey-ed-claude-diagnosing/<platform>_*  (diagnosing flags, retry counters)
      - /tmp/taey-ed-consult/consult_*  (open consults — abandoned)
      - pending_validations for this platform

    Preserved:
      - hash_index (learned screen → variant mappings)
      - variant_cache (canonical BTs)
      - knowledge.json operational_notes
      - signatures (learned screen signatures)
    """
    from pathlib import Path
    import shutil
    cleared = {"diag_dirs": 0, "consults": 0, "pending_validations": 0}

    # 1. Diagnosing state dirs for this platform
    diag_root = Path("/tmp/taey-ed-claude-diagnosing")
    if diag_root.exists():
        for d in diag_root.glob(f"{platform}_*"):
            try:
                shutil.rmtree(d)
                cleared["diag_dirs"] += 1
            except Exception:
                logger.exception(f"failed to clear diag dir {d}")

    # 2. Open consults
    consult_root = Path("/tmp/taey-ed-consult")
    if consult_root.exists():
        for d in consult_root.glob("consult_*"):
            try:
                shutil.rmtree(d)
                cleared["consults"] += 1
            except Exception:
                logger.exception(f"failed to clear consult {d}")

    # 3. Pending validations for this platform
    pv_root = Path("/home/user/taey-ed-data/pending_validations") / platform
    if pv_root.exists():
        for f in pv_root.glob("*.json"):
            try:
                f.unlink()
                cleared["pending_validations"] += 1
            except Exception:
                logger.exception(f"failed to clear pending validation {f}")

    logger.warning(
        f"SESSION RESET for {platform}: cleared "
        f"{cleared['diag_dirs']} diag dirs, "
        f"{cleared['consults']} consults, "
        f"{cleared['pending_validations']} pending validations. "
        f"hash_index + variant_cache + knowledge.json PRESERVED."
    )
    return {"ok": True, "platform": platform, "cleared": cleared}


# ── Hydration guard state (claude-0 2026-06-13) ──
# Bounded re-poll when a capture is exercise-shaped but its answer widgets
# haven't hydrated yet. Per-platform counter in the long-lived API process;
# reset whenever a non-hydrating capture arrives so it can't accumulate.
MAX_HYDRATION_WAITS = 6
_HYDRATION_WAITS: dict = {}


def _bump_hydration_wait(platform: str) -> int:
    _HYDRATION_WAITS[platform] = _HYDRATION_WAITS.get(platform, 0) + 1
    return _HYDRATION_WAITS[platform]


def _reset_hydration_wait(platform: str) -> None:
    _HYDRATION_WAITS.pop(platform, None)


def _looks_like_hydrating_exercise(tree: dict) -> bool:
    """True when the capture shows exercise content (a graded submit button +
    substantial question text) but NO answer-input widgets — i.e. the widgets
    are still hydrating. Conservative: requires the submit affordance AND
    content text AND zero answer widgets, so non-exercise screens never match.
    """
    from spark.tasks.classify_screen import _find_web_area

    web = _find_web_area(tree) or tree
    has_submit = False
    answer_widgets = 0
    text_chars = 0

    def walk(n):
        nonlocal has_submit, answer_widgets, text_chars
        if isinstance(n, dict):
            role = n.get("role") or ""
            name = (n.get("name") or "").strip()
            if role == "AXButton" and name in ("Check", "Submit", "Check answer", "Check again"):
                has_submit = True
            if role in ("AXComboBox", "AXRadioButton", "AXCheckBox", "AXTextField", "AXTextArea", "AXSlider"):
                answer_widgets += 1
            if role == "AXStaticText":
                text_chars += len(name) + len(str(n.get("value") or ""))
            for c in n.get("children") or []:
                walk(c)
        elif isinstance(n, list):
            for i in n:
                walk(i)

    walk(web)
    return has_submit and answer_widgets == 0 and text_chars > 80


@router.post("/next_action")
def next_action(request: NextActionRequest):
    """Production wrapper: intercept any user_input_needed at the endpoint
    boundary and route through claude_diagnosing instead. Per Jesse 2026-05-18:
    Mac app + API do not escalate to user. Everything that isn't perfectly
    handled goes to the Mira-side Claude diagnosis loop.

    The only path to a real user_input_needed directive is when a Mira-side
    Claude has explicitly created a gave_up.flag in the diagnosis state dir
    — that is handled inside _escalate_to_claude_diagnosing, not here.
    """
    # PRE-PIPELINE CHECK: GLOBAL "waiting on central guidance" lock.
    #
    # Per Jesse 2026-05-18: any time the system is waiting on central feedback
    # — for ANY consultation or diagnosis cycle, on ANY platform — Mac should
    # do nothing but wait. The previous per-(platform, hash) check leaked when
    # Mac's tree mutated mid-diagnosis (animation, page auto-advance) and the
    # new hash escaped the lock, cascading into fresh consultations.
    #
    # "Waiting on central guidance" = either of:
    #   1. An open consultation: /tmp/taey-ed-consult/consult_*/ with no
    #      response.json yet (the worker is still computing or pending).
    #   2. An active diagnosis: /tmp/taey-ed-claude-diagnosing/*/ with a
    #      diagnosing.flag and no diagnosis_done.flag / gave_up.flag.
    #
    # While either condition holds anywhere, every /next_action returns wait.
    # Holds across platforms, screens, and tree mutations. Prevents cascade.
    try:
        _waiting_reasons = []

        # Condition 1: open consultations (response.json missing)
        _consult_root = Path("/tmp/taey-ed-consult")
        if _consult_root.exists():
            for _consult_dir in _consult_root.iterdir():
                if not _consult_dir.is_dir() or not _consult_dir.name.startswith("consult_"):
                    continue
                if (_consult_dir / "response.json").exists():
                    continue
                # Check metadata — abandoned/stale consults shouldn't lock us
                _meta = _consult_dir / "metadata.json"
                if _meta.exists():
                    try:
                        _m = json.loads(_meta.read_text())
                        if _m.get("status") == "abandoned":
                            continue
                    except Exception:
                        pass
                _waiting_reasons.append(f"open_consult:{_consult_dir.name}")
                if len(_waiting_reasons) >= 4:
                    break

        # Condition 2: active diagnosis cycles
        _diag_root = Path("/tmp/taey-ed-claude-diagnosing")
        if _diag_root.exists():
            for _state_dir in _diag_root.iterdir():
                if not _state_dir.is_dir():
                    continue
                if (_state_dir / "diagnosing.flag").exists() \
                        and not (_state_dir / "diagnosis_done.flag").exists() \
                        and not (_state_dir / "gave_up.flag").exists():
                    _waiting_reasons.append(f"diagnosing:{_state_dir.name}")
                    if len(_waiting_reasons) >= 4:
                        break

        if _waiting_reasons:
            logger.info(
                f"PRE-PIPELINE: {len(_waiting_reasons)} central-feedback wait(s) active "
                f"({', '.join(_waiting_reasons[:3])}"
                f"{'...' if len(_waiting_reasons) > 3 else ''}) "
                f"— returning wait, skipping pipeline"
            )
            return {
                "directive": "wait",
                "directive_id": _make_directive_id(),
                "seconds": 30.0,
                "reason": "central_feedback_pending",
                "message": (
                    f"Waiting on central guidance ({len(_waiting_reasons)} cycle(s)) — "
                    f"Mac will retry automatically."
                ),
            }
    except Exception:
        logger.exception("PRE-PIPELINE central-feedback check failed (non-fatal)")

    response = _next_action_impl(request)
    if isinstance(response, dict) and response.get("directive") == "user_input_needed":
        platform = request.platform
        tree = request.tree
        # Best-effort consultation_id from client state, then any response field
        cs = request.client_state or ClientState()
        consult_id = (
            (cs.active_consultation_id or "")
            or response.get("consultation_id", "")
            or ""
        )
        reason = response.get("reason", "user_input_needed intercepted by wrapper")
        screen_type_hint = response.get("screen_type", "UNKNOWN")
        # Forward Mac BT execution log + failed BT into the diagnosis ping
        lr = request.last_result
        bt_debug_tail = (lr.bt_debug_tail or "") if lr else ""
        failed_bt = lr.failed_bt if lr and lr.failed_bt else None
        logger.warning(
            f"WRAPPER: intercepted user_input_needed → claude_diagnosing "
            f"(consult={consult_id!r}, screen={screen_type_hint!r}, "
            f"bt_log={'yes' if bt_debug_tail else 'no'}, failed_bt={'yes' if failed_bt else 'no'})"
        )
        return _escalate_to_claude_diagnosing(
            platform=platform,
            tree=tree,
            consultation_id=consult_id,
            reason=reason,
            screen_type_hint=screen_type_hint,
            bt_debug_tail=bt_debug_tail,
            failed_bt=failed_bt,
        )
    return response


def _next_action_impl(request: NextActionRequest):
    """
    Directive Model: Mac sends state, Spark returns ONE directive.

    Replaces /match + /consult + /validate + /action_review from Mac's perspective.
    """
    platform = request.platform
    tree = request.tree
    cs = request.client_state or ClientState()
    lr = request.last_result

    # Store incoming user chat message (proactive or response)
    if request.chat_message:
        store_message(platform, build_user_message(request.chat_message))
        logger.info(f"  Chat: stored user message: {request.chat_message[:80]}")

        # ── URGENT: User message = STOP EVERYTHING + submit to claude-primary NOW ──
        # Jesse 2026-06-01: "If a user says something, that usually means you did
        # something wrong and whatever is going on needs to stop immediately and
        # be submitted to you." So a user message routes STRAIGHT to the
        # escalate-to-claude-primary path — regardless of whether a screenshot is
        # attached, and NEVER queued to the worker. (Old behavior gated the
        # override on request.screenshot_b64 and routed to the worker consult,
        # so a no-screenshot message just fell through to normal flow = "queued".)
        _ug = [f"USER MESSAGE (URGENT — stop everything and address immediately): {request.chat_message}"]
        if lr:
            _ug.append(f"Previous screen: {lr.screen or 'unknown'} | action: {lr.action or 'unknown'} | success: {lr.success}")
            if lr.bt_debug_tail:
                _ug.append(f"Last BT debug:\n{lr.bt_debug_tail}")
        return _escalate_to_claude_diagnosing(
            platform=platform,
            tree=tree,
            consultation_id="",
            reason="\n".join(_ug),
            screen_type_hint=((lr.screen if lr else None) or "USER_MESSAGE"),
            screenshot_b64=request.screenshot_b64,
        )

    logger.info(
        f">>> /next_action: platform={platform} "
        f"course_id={cs.course_id} "
        f"has_screenshot={'yes' if request.screenshot_b64 else 'no'} "
        f"has_last_result={'yes' if lr else 'no'} "
        f"active_consultation={cs.active_consultation_id or 'none'}"
    )
    if lr:
        logger.info(
            f"    last_result: success={lr.success} screen={lr.screen} "
            f"action={lr.action} continue_loop={lr.continue_loop} "
            f"hash_before={lr.tree_hash_before[:12] if lr.tree_hash_before else 'none'} "
            f"hash_after={lr.tree_hash_after[:12] if lr.tree_hash_after else 'none'}"
        )
        if lr.bt_debug_tail:
            logger.info(f"    bt_debug_tail:\n{lr.bt_debug_tail}")

    # ── Step 0.5: Wrong-window guard ──
    # If the platform's knowledge declares web_area_markers and the captured
    # WebArea title matches none of them, the Mac captured a NON-course window
    # (observed live 2026-06-11: JupyterLab captured twice, got hashed into
    # the khan map and escalated). Junk input must not reach validation,
    # hashing, classification, or escalation — return wait and self-heal
    # when the course window regains focus.
    if tree:
        try:
            from spark.tasks.knowledge_loader import load_knowledge as _lk_ww
            _markers = (_lk_ww(platform) or {}).get(
                "global", {}).get("web_area_markers") or []
            if _markers:
                from spark.tasks.prompt_codex import _find_web_area as _fwa_ww
                _wa = _fwa_ww(tree) or {}
                _wa_title = str(_wa.get("name") or "")
                if _wa_title and not any(
                    m.lower() in _wa_title.lower() for m in _markers
                ):
                    logger.warning(
                        f"  Step 0.5: WRONG WINDOW — WebArea {_wa_title!r} matches no "
                        f"web_area_markers for {platform}. Waiting for course window."
                    )
                    return _with_chat({
                        "directive": "wait",
                        "directive_id": _make_directive_id(),
                        "seconds": 10.0,
                        "reason": "wrong_window",
                        "message": (
                            f"Captured window is '{_wa_title[:60]}', not {platform}. "
                            f"Bring the course window to the front."
                        ),
                    }, platform, [
                        build_status(
                            f"Wrong window focused ('{_wa_title[:40]}') — "
                            f"bring the course window to the front"
                        ),
                    ])
        except Exception as _ww_exc:
            logger.warning(f"  Step 0.5: wrong-window guard error (continuing): {_ww_exc}")

    # ── Step 1: Active consultation? Check if done ──
    logger.info("  Step 1: Checking active consultation...")
    consultation_id = cs.active_consultation_id
    if consultation_id:
        consult_status = check_consultation(consultation_id)
        if consult_status.get("status") == "complete":
            # Worker-fallback contract: when the consultation worker fails to
            # generate a BT, it writes a response.json with _worker_fallback=True
            # and an inert wait BT. Convert that here to a real user_input_needed
            # directive — the Mac should prompt the user, not execute the wait.
            if consult_status.get("_worker_fallback"):
                _wf_reason = consult_status.get(
                    "_worker_failure_reason", "worker failed to generate BT"
                )
                logger.error(
                    f"Step 1: worker_fallback for {consultation_id} — "
                    f"routing through claude_diagnosing helper. reason={_wf_reason}"
                )
                # Route through the single canonical helper. It checks done.flag
                # (abandons stale consult, signals retry), gave_up.flag (user
                # fallback), or notifies and returns wait if neither.
                return _escalate_to_claude_diagnosing(
                    platform=platform,
                    tree=tree,
                    consultation_id=consultation_id,
                    reason=f"worker_fallback: {_wf_reason}",
                    screen_type_hint=consult_status.get("screen_type", "UNKNOWN"),
                    bt_debug_tail=(lr.bt_debug_tail or "") if lr else "",
                    failed_bt=lr.failed_bt if lr and lr.failed_bt else None,
                )
            tree_def = consult_status.get("tree")
            if tree_def:
                # Bug #8: Get skeleton_hash from CURRENT tree, not stale consultation
                _consult_skeleton_hash = ""
                try:
                    _consult_tree_file = Path("/tmp/taey-ed-consult") / consultation_id / "tree.json"
                    if _consult_tree_file.exists():
                        from spark.tasks.skeleton import extract_skeleton, skeleton_hash as _skel_hash
                        _ax_tree = json.loads(_consult_tree_file.read_text())
                        _skel = extract_skeleton(_ax_tree)
                        _consult_skeleton_hash = _skel_hash(_skel)
                except Exception:
                    pass

                _screen = consult_status.get("screen_type", "CONSULTATION")
                return _with_chat({
                    "directive": "execute_tree",
                    "directive_id": _make_directive_id(),
                    "tree": tree_def,
                    "screen": _screen,
                    "extract": consult_status.get("extract"),
                    "course_id": consult_status.get("course_id", cs.course_id),
                    "lesson": "",
                    "expected_next": consult_status.get("expected_next", []),
                    "skeleton_hash": _consult_skeleton_hash,
                }, platform, [build_status(f"Consultation resolved — executing {_screen} automation")])
            logger.warning(f"Consultation {consultation_id} complete but no tree, re-matching")
        elif consult_status.get("status") in ("not_found", "abandoned"):
            logger.warning(
                f"Consultation {consultation_id} {consult_status.get('status')}, "
                f"clearing and re-matching"
            )
            # Fall through to Step 4 (match screen) instead of waiting forever.
            # 'abandoned' is the state set by _escalate_to_claude_diagnosing when
            # done.flag is consumed — gets the pipeline running fresh with
            # updated knowledge.json instead of polling the stale consult.
        else:
            return {
                "directive": "wait",
                "directive_id": _make_directive_id(),
                "seconds": 5.0,
                "reason": "consulting",
            }

    # ── Step 2: Validate previous action result ──
    logger.info("  Step 2: Validating previous action...")
    if lr and lr.success and lr.after_tree and not lr.continue_loop:
        vr = _validate_last_action(platform, lr, tree)

        if vr["validated"]:
            if lr.directive_skeleton_hash:
                try:
                    from spark.tasks.variant_cache import mark_variant_validated, mark_hash_validated
                    mark_hash_validated(platform=platform, skel_hash=lr.directive_skeleton_hash)
                    if lr.screen:
                        mark_variant_validated(platform=platform, variant=lr.screen)
                    logger.info(
                        f"Step 2: Validated {lr.screen} "
                        f"(hash={lr.directive_skeleton_hash[:12]}, "
                        f"new_screen={vr.get('new_screen')})"
                    )
                except Exception as e:
                    logger.warning(f"Step 2: mark_validated failed (non-fatal): {e}")

            # Success: clear failure tracking for this platform
            _clear_variant_failures(platform)

            # Screen session: the screen ADVANCED — record + archive its
            # working memory (Jesse 2026-06-11: stored until the screen
            # advances).
            if lr.directive_skeleton_hash:
                try:
                    from spark.tasks.screen_session import record_attempt, archive
                    record_attempt(
                        platform, lr.directive_skeleton_hash,
                        bt_actions=_bt_actions_from_tail(lr.bt_debug_tail),
                        outcome="advanced",
                        detail=f"validated; new_screen={vr.get('new_screen')}",
                        author="machine",
                    )
                    archive(platform, lr.directive_skeleton_hash)
                except Exception:
                    logger.exception("screen_session advance-archive failed (continuing)")

            # Learn from successful BT execution
            # Note: lr doesn't carry the BT definition — pass empty dict.
            # submit_button detection won't fire but screen_type + success is recorded.
            learn_from_bt_result(
                platform=platform,
                screen_type=lr.screen or "UNKNOWN",
                bt_result={"success": True, "screen_type": lr.screen},
                bt_definition={},
                skeleton_hash=lr.directive_skeleton_hash or "",
            )

            # Pending-validation notify (Jesse 2026-05-19): if the
            # just-executed directive came from an unvalidated screen map
            # (variant_cache or signature), ping claude-primary with the
            # outcome so the entry can be mark_validated / delete_screen'd.
            try:
                from spark.tasks.validation_tracker import (
                    check_pending, notify_claude_for_validation,
                    clear_pending, _summarize_tree,
                )
                pending = check_pending(platform, lr.directive_skeleton_hash or "")
                if pending:
                    notify_claude_for_validation(
                        record=pending,
                        last_result={
                            "success": lr.success,
                            "screen": lr.screen,
                            "tree_hash_before": lr.tree_hash_before,
                            "tree_hash_after": lr.tree_hash_after,
                            "bt_debug_tail": lr.bt_debug_tail or "",
                        },
                        after_tree_summary=_summarize_tree(tree),
                    )
                    # Clear so we don't re-notify on the next poll. claude-
                    # primary's response (mark_validated or delete) is the
                    # canonical resolution; the pending file is only the
                    # one-shot notification trigger.
                    clear_pending(platform, lr.directive_skeleton_hash or "")
            except Exception:
                logger.exception("Step 2 validation notify failed (non-fatal)")

        elif vr["wrong_answer"]:
            # ONE TRY ONLY: Wrong answer means the action was wrong. STOP.
            logger.error(
                f"Step 2: WRONG ANSWER for {lr.screen} — "
                f"ONE TRY ONLY — stopping. Hash={lr.directive_skeleton_hash}"
            )
            deleted_failed_map = False
            if lr.directive_skeleton_hash:
                try:
                    from spark.tasks.variant_cache import (
                        delete_hash,
                        invalidate_variant_bt,
                        lookup_by_hash,
                    )
                    _record_screen_failure(platform, lr.directive_skeleton_hash,
                                           lr.bt_debug_tail, "wrong_answer")
                    hash_entry = lookup_by_hash(platform, lr.directive_skeleton_hash)
                    if hash_entry and hash_entry.get("validated"):
                        from spark.tasks.variant_cache import record_validated_map_failure
                        record_validated_map_failure(
                            platform, lr.directive_skeleton_hash, lr.screen,
                        )
                        logger.info(
                            f"Step 2: VALIDATED map {lr.screen} (hash "
                            f"{lr.directive_skeleton_hash[:12]}) got a wrong answer — "
                            f"KEEPING map (demotes at 2 consecutive) and escalating"
                        )
                    else:
                        delete_hash(platform=platform, skel_hash=lr.directive_skeleton_hash)
                        if lr.screen:
                            invalidate_variant_bt(platform=platform, variant=lr.screen)
                        deleted_failed_map = True
                except Exception as e:
                    logger.warning(f"Step 2: delete_hash failed: {e}")
            from spark.tasks.classify_screen import _describe_screen
            _reason = (f"WRONG ANSWER on screen '{lr.screen}'. "
                       f"Tell me what the correct action is for this screen.")
            return _with_chat({
                "directive": "user_input_needed",
                "directive_id": _make_directive_id(),
                "reason": _reason,
                "screen_type": lr.screen or "UNKNOWN",
                "screen_description": _describe_screen(tree),
            }, platform, [
                build_status(
                    f"Wrong answer detected on {lr.screen} — "
                    f"{'deleted unvalidated approach' if deleted_failed_map else 'kept validated map and escalated'}"
                ),
                build_question(_reason),
            ])

        elif vr.get("reason") == "not_advanced":
            # NOT-ADVANCED IS A FAILURE, not a neutral (2026-06-12: this path
            # was a polite infinite loop — three ranking builds in 20 minutes,
            # each executed, each left the question unmoved, each fell through
            # to a fresh rebuild with no attempt counted and no escalation).
            # An exercise BT that ran 'successfully' and provably changed
            # nothing accomplished nothing — same class as stuck: record it
            # and climb the ladder.
            logger.error(
                f"Step 2: NOT-ADVANCED for {lr.screen} — exercise BT ran but the "
                f"question did not move. Escalating (ladder counts this attempt)."
            )
            _record_screen_failure(platform, lr.directive_skeleton_hash,
                                   lr.bt_debug_tail, "not_advanced",
                                   "BT 'succeeded' but same skeleton — nothing staged/accepted")
            return _escalate_to_claude_diagnosing(
                platform=platform,
                tree=tree,
                consultation_id="",
                reason=(f"NOT-ADVANCED on '{lr.screen}': the BT executed without "
                        f"error but the question did not move — actions likely "
                        f"never engaged the widget (drags missed / Check disabled)."),
                screen_type_hint=lr.screen or "EXERCISE",
                bt_debug_tail=lr.bt_debug_tail or "",
                failed_bt=lr.failed_bt,
                screenshot_b64=request.screenshot_b64,
            )
        else:
            logger.warning(
                f"Step 2: Validation failed for {lr.screen}: {vr.get('reason')} "
                f"(new_screen={vr.get('new_screen')}, "
                f"expected_next_match={vr.get('expected_next_match')})"
            )

    # ── Step 2.5: Stuck detection ──
    # ONE TRY ONLY: If the action didn't change the screen, STOP.
    # Do NOT re-classify, do NOT create a consultation. Just stop.
    logger.info("  Step 2.5: Checking for stuck screen...")
    if lr and lr.success and not lr.continue_loop and lr.screen:
        tree_hash_changed = lr.tree_hash_before != lr.tree_hash_after
        if not tree_hash_changed and _is_scroll_only_bt(lr.bt_debug_tail):
            # Phase-1 of the two-phase drag pattern: a scroll/wait-only BT is
            # EXPECTED to leave the screen unchanged — the point is the next
            # capture carries on-screen coordinates for phase-2. Twice today
            # (dropdown 14:11, matcher 14:20) this tripped stuck-detection and
            # burned ladder attempts on a working flow. Fall through to a
            # fresh build, no failure recorded.
            logger.info(
                f"  Step 2.5: scroll-only BT, unchanged screen — PHASE COMPLETE "
                f"(not stuck). Rebuilding from post-scroll capture."
            )
            _record_screen_failure(platform, lr.directive_skeleton_hash,
                                   lr.bt_debug_tail, "scroll_phase_complete",
                                   "screen unchanged by design; phase-2 next")
        elif not tree_hash_changed:
            logger.error(
                f"STUCK: {lr.screen} unchanged after action. "
                f"ONE TRY ONLY — stopping. Hash={lr.directive_skeleton_hash or 'none'}"
            )
            _record_screen_failure(platform, lr.directive_skeleton_hash,
                                   lr.bt_debug_tail, "stuck",
                                   "tree unchanged after non-scroll action")
            deleted_failed_map = False
            # Delete the failed hash mapping so it doesn't re-match next time
            if lr.directive_skeleton_hash:
                try:
                    from spark.tasks.variant_cache import (
                        delete_hash as _del_hash,
                        invalidate_variant_bt as _inv_bt,
                        lookup_by_hash as _lookup_hash,
                    )
                    _entry = _lookup_hash(platform, lr.directive_skeleton_hash)
                    if _entry and _entry.get("validated"):
                        from spark.tasks.variant_cache import (
                            record_validated_map_failure as _rec_fail,
                        )
                        _rec_fail(platform, lr.directive_skeleton_hash, lr.screen)
                        logger.info(
                            f"Step 2.5: VALIDATED map {lr.screen} (hash "
                            f"{lr.directive_skeleton_hash[:12]}) got stuck — "
                            f"KEEPING map (demotes at 2 consecutive) and escalating"
                        )
                    else:
                        _del_hash(platform=platform, skel_hash=lr.directive_skeleton_hash)
                        if lr.screen:
                            _inv_bt(platform=platform, variant=lr.screen)
                        deleted_failed_map = True
                        logger.info(
                            f"Step 2.5: Deleted unvalidated failed hash "
                            f"{lr.directive_skeleton_hash[:12]}"
                        )
                except Exception as e:
                    logger.warning(f"Step 2.5: delete_hash failed: {e}")
            from spark.tasks.classify_screen import _describe_screen
            _reason = (f"STUCK: Screen '{lr.screen}' unchanged after action. "
                       f"Tell me what to do on this screen.")
            return _with_chat({
                "directive": "user_input_needed",
                "directive_id": _make_directive_id(),
                "reason": _reason,
                "screen_type": lr.screen or "UNKNOWN",
                "screen_description": _describe_screen(tree),
            }, platform, [
                build_status(
                    f"Screen unchanged after action on {lr.screen} — "
                    f"{'deleted unvalidated approach' if deleted_failed_map else 'kept validated map and need your help'}"
                ),
                build_question(_reason),
            ])

    # ── Step 2.7: Polling completion detection ──
    logger.info("  Step 2.7: Checking polling completion...")
    if lr and lr.continue_loop and lr.tree_hash_before and lr.tree_hash_after:
        # 2026-05-15: Deleted the unchanged-tree unconditional re-poll branch.
        # That branch (introduced 2026-05-14 in 5f5485f) had no exit condition
        # and would loop forever on Khan video screens where the SKELETON is
        # stable across video states (Pause -> Replay is a text-value change,
        # not a structural change). With this branch removed, tree-unchanged
        # falls through naturally to Step 4 exact-hash lookup, which hits the
        # cached video_poll BT during playback (cheap) and falls to Step 5
        # classification when the structure finally changes at completion.
        # See consultations/04_VIDEO_POLL_ARCHAEOLOGY.md Section D1.
        # Completion check runs on EVERY poll cycle, independent of hash
        # movement: a FINISHED video's tree stops changing entirely (live
        # 2026-06-11 18:04 — hash static at 4848e6f0, detector unreachable
        # inside the tree-changed branch, poll forever). During playback the
        # hash changes (timestamps); at completion it freezes. Either way,
        # the indicators in the CURRENT tree are the truth.
        from spark.tasks.screen_type_util import get_master_category as _gmc_poll
        _is_video_poll = bool(lr.screen and _gmc_poll(lr.screen or "") == "VIDEO")
        if lr.tree_hash_before != lr.tree_hash_after or _is_video_poll:
            is_video = _is_video_poll
            if is_video:
                # COMPLETION INDICATORS decide, not player presence: the
                # YouTube player stays in the tree after completion, so
                # HAS_VIDEO was true forever and a finished video polled
                # endlessly (live 2026-06-11, Wave properties: seek at 100,
                # 'Replay Video', sidebar 'completed Video' — still polling).
                # Per Jesse's validated law, playing vs complete is NEVER
                # identical — there is always an indicator.
                def _video_complete(n) -> bool:
                    if not isinstance(n, dict):
                        return False
                    name = str(n.get("name") or n.get("title") or "")
                    role = n.get("role") or ""
                    if role == "AXButton" and name == "Replay Video":
                        return True
                    if role == "AXLink" and name.startswith("completed Video"):
                        return True
                    if role == "AXSlider" and "seek" in name.lower():
                        try:
                            if float(n.get("value") or 0) >= 99.5:
                                return True
                        except (TypeError, ValueError):
                            pass
                    return any(_video_complete(c) for c in n.get("children") or [])

                video_done = _video_complete(tree)
                from spark.tasks.prompt_codex import analyze_tree as _analyze
                current_tags = _analyze(tree)
                if "HAS_VIDEO" in current_tags and not video_done:
                    logger.info(
                        f"Step 2.7: Video still playing (player present, no "
                        f"completion indicator). Continuing poll — NOT advancing."
                    )
                    # Return video_poll again to keep watching
                    return {
                        "directive": "execute_tree",
                        "directive_id": _make_directive_id(),
                        "tree": {
                            "type": "sequence",
                            "children": [
                                {"type": "action", "action": "video_poll"},
                            ],
                        },
                        "screen": lr.screen,
                        "course_id": cs.course_id,
                    }
                else:
                    logger.info(
                        f"Step 2.7: Video screen no longer has video signals. "
                        f"Video completed — navigating forward."
                    )

            logger.info(
                f"Content completed: tree changed after {lr.action} "
                f"(screen={lr.screen}). Building advancement BT via Gemini."
            )
            # Button names vary by platform — let Gemini read the actual tree
            if not request.screenshot_b64:
                return {
                    "directive": "need_screenshot",
                    "directive_id": _make_directive_id(),
                    "reason": "content_complete_advance",
                }
            return _build_screen_directive(
                request, platform, tree, "TRANSITION__SUMMARY", "",
                user_guidance=(
                    "Content (video/article) just completed. Advance to the next item.\n"
                    "Find the explicit forward-advancement link in the AX tree — typically:\n"
                    "  - an AXLink whose name starts with 'Up next:' (read the FULL exact name from the tree, including the title after the colon)\n"
                    "  - OR a button labeled 'Continue', 'Next question', 'Done', 'Mark complete'\n"
                    "Use find_and_click with the EXACT name read from the tree + match_mode=exact + appropriate role (AXLink or AXButton). Pin the dynamic part (e.g., the video title after 'Up next:') verbatim — do NOT use match_mode=contains.\n"
                    "DO NOT click small course-wide breadcrumb arrows named 'Next in course' / 'Previous in course' at the top of the page — those are course-wide chrome that skip past entire units, never the right advancement target.\n"
                    "DO NOT click 'Up next for you!' algorithmic-recommendation cards — those are Khan's personalized suggestions, not linear course progression. The correct 'Up next' is the AXLink in the lower-right of the video player area.\n"
                    "If neither an 'Up next:' link nor a Continue/Next button is visible in the tree, the next lesson item in the LEFT SIDEBAR (look for the first un-checkmarked sidebar AXLink) is the fallback target."
                ),
                course_id=cs.course_id,
            )

    # ── Step 3: Previous action failed? ──
    logger.info("  Step 3: Checking for previous failure...")
    if lr and lr.success is False:
        if lr.user_response:
            # User provided guidance — build BT directly using Gemini
            logger.info(
                f"Step 3: User guidance received for {lr.screen}: "
                f"'{lr.user_response[:100]}'"
            )
            if not request.screenshot_b64:
                return {
                    "directive": "need_screenshot",
                    "directive_id": _make_directive_id(),
                    "reason": "user_guidance_needs_screenshot",
                }

            consult_result = request_consultation(
                platform=platform,
                tree=tree,
                screenshot_b64=request.screenshot_b64,
                context={
                    "screen_type": lr.screen or "UNKNOWN",
                    "screen_type_hint": lr.screen or "UNKNOWN",
                    "user_guidance": lr.user_response,
                    "failed_screen": lr.screen,
                    "course_id": cs.course_id,
                },
                bt_debug_log=lr.bt_debug_tail or "",
            )
            return _consultation_or_wait(consult_result)

        # If Mac sent the failed BT, try ONE Gemini call with failure context
        if lr.failed_bt and request.screenshot_b64:
            logger.info(
                f"Step 3: BT failed for {lr.screen} — retrying with failure context"
            )

            consult_result = request_consultation(
                platform=platform,
                tree=tree,
                screenshot_b64=request.screenshot_b64,
                context={
                    "screen_type": lr.screen or "UNKNOWN",
                    "screen_type_hint": lr.screen or "UNKNOWN",
                    "failure_reason": "bt_failed",
                    "previous_screen": lr.screen or "",
                    "failed_bt": lr.failed_bt,
                    "course_id": cs.course_id,
                },
                bt_debug_log=lr.bt_debug_tail or "",
            )
            return _consultation_or_wait(consult_result)

        # ── Screen mismatch recovery ──
        # Record failure to prevent reclassify loops
        if lr.screen:
            _record_variant_failure(platform, lr.screen)

        # If the BT came from a hash match and it failed (including Gemini
        # retry), the hash mapping was likely wrong. Delete it.
        # BUT: check failure count BEFORE falling through to reclassify.
        _record_screen_failure(platform, lr.directive_skeleton_hash,
                               lr.bt_debug_tail, "bt_failure",
                               (lr.bt_debug_tail or "")[-200:])
        if lr.directive_skeleton_hash:
            try:
                from spark.tasks.variant_cache import (
                    delete_hash as _del_hash3, invalidate_variant_bt as _inv_bt3,
                    lookup_by_hash as _lookup_hash3,
                )
                _entry3 = _lookup_hash3(platform, lr.directive_skeleton_hash)
                if _entry3 and _entry3.get("validated"):
                    # Root-cause guard (Jesse 2026-06-01): a VALIDATED map represents
                    # "map once, recognized forever". A single transient/cross-state BT
                    # failure (e.g. an advance button momentarily absent from the tree,
                    # or the screen having moved between match and execute) must NOT
                    # destroy it — that turns recognition into one-shot-then-forgotten.
                    # Keep the map; escalate for review instead of deleting.
                    # record_validated_map_failure demotes (validated=False +
                    # note debit) at 2 consecutive failures — INTENDED_FLOW §E.
                    from spark.tasks.variant_cache import (
                        record_validated_map_failure as _rec_fail3,
                    )
                    _rec_fail3(platform, lr.directive_skeleton_hash, lr.screen)
                    logger.info(
                        f"Step 3: VALIDATED map {lr.screen} (hash "
                        f"{lr.directive_skeleton_hash[:12]}) failed — KEEPING map "
                        f"(demotes at 2 consecutive), escalating for review"
                    )
                else:
                    logger.info(
                        f"Step 3: BT failed for hash-matched screen {lr.screen} "
                        f"— deleting unvalidated hash {lr.directive_skeleton_hash[:12]}"
                    )
                    _del_hash3(platform=platform, skel_hash=lr.directive_skeleton_hash)
                    if lr.screen:
                        _inv_bt3(platform=platform, variant=lr.screen)
            except Exception as e:
                logger.warning(f"Step 3: delete_hash guard failed: {e}")

            # Check if this variant has failed too many times — DON'T reclassify
            if lr.screen and _check_variant_failed(platform, lr.screen):
                logger.error(
                    f"Step 3: LOOP GUARD — {lr.screen} has failed {MAX_VARIANT_FAILURES}+ times. "
                    f"STOPPING to prevent infinite reclassify loop."
                )
                from spark.tasks.classify_screen import _describe_screen
                _reason = (f"Screen '{lr.screen}' has failed {MAX_VARIANT_FAILURES}+ times. "
                           f"The automation cannot handle this screen type. "
                           f"Tell me what to do.")
                return _with_chat({
                    "directive": "user_input_needed",
                    "directive_id": _make_directive_id(),
                    "reason": _reason,
                    "screen_type": lr.screen or "UNKNOWN",
                    "screen_description": _describe_screen(tree),
                }, platform, [
                    build_status(f"Repeated failure on {lr.screen} — need your help"),
                    build_question(_reason),
                ])

            # Fall through to Step 4/5 for fresh classification
        else:
            # No signature to invalidate — genuinely stuck. STOP.
            bt_diag = ""
            if lr.bt_debug_tail:
                bt_diag = f"\nBT debug log:\n{lr.bt_debug_tail}"
                logger.error(
                    f"Step 3: BT execution failed for {lr.screen}, action={lr.action}. "
                    f"Stopping.{bt_diag}"
                )
            else:
                logger.error(
                    f"Step 3: BT execution failed for {lr.screen}, action={lr.action}. "
                    f"Stopping. (no bt_debug_tail)"
                )
            from spark.tasks.classify_screen import _describe_screen
            _reason = (f"Action '{lr.action}' failed on screen '{lr.screen}'. "
                       f"Tell me what to do on this screen.")
            return _with_chat({
                "directive": "user_input_needed",
                "directive_id": _make_directive_id(),
                "reason": _reason,
                "screen_type": lr.screen or "UNKNOWN",
                "screen_description": _describe_screen(tree),
                "bt_debug_tail": lr.bt_debug_tail or "",
            }, platform, [
                build_status(f"Action failed on {lr.screen}"),
                build_question(_reason),
            ])

    # ── Step 4: V21 Exact hash lookup ──
    from spark.tasks.skeleton import extract_skeleton, skeleton_hash as _skel_hash
    from spark.tasks.variant_cache import (
        lookup_by_hash, lookup_variant_bt, register_hash, delete_hash,
        mark_variant_validated, mark_hash_validated,
    )

    skel = extract_skeleton(tree)  # web_content_only=True by default
    skel_hash = _skel_hash(skel)
    logger.info(f"  Step 4: Exact hash lookup (hash={skel_hash[:12]})")

    hash_result = lookup_by_hash(platform, skel_hash)
    if hash_result:
        variant = hash_result["variant"]
        logger.info(f"  Step 4: Hash hit → variant={variant}")

        # Per Jesse 2026-05-19: every screen-map use of an UNVALIDATED entry
        # must notify claude-primary for validation after Mac executes the BT.
        # Record pending now; Step 2 on next /next_action picks it up.
        if not hash_result.get("validated", False):
            try:
                from spark.tasks.validation_tracker import record_pending
                record_pending(
                    platform=platform,
                    skel_hash=skel_hash,
                    variant=variant,
                    source="variant_cache",
                    tree=tree,
                )
                logger.info(
                    f"  Step 4: recorded pending validation for unvalidated "
                    f"variant_cache entry {skel_hash[:12]} → {variant}"
                )
            except Exception:
                logger.exception("Step 4 record_pending failed (non-fatal)")

        bt_entry = lookup_variant_bt(platform, variant)
        if bt_entry and bt_entry.get("behavior_tree"):
            stored_bt = bt_entry["behavior_tree"]
            bt_json = json.dumps(stored_bt, indent=2)
            logger.info(f"  Step 4: REUSING BT for {variant} (hash={skel_hash[:12]})")
            logger.info(f"  Stored BT:\n{bt_json}")
            return _with_chat({
                "directive": "execute_tree",
                "directive_id": _make_directive_id(),
                "tree": stored_bt,
                "screen": variant,
                "skeleton_hash": skel_hash,
                "extract": bt_entry.get("extract"),
                "expected_next": bt_entry.get("expected_next", []),
                "course_id": cs.course_id,
            }, platform, [build_status(f"Executing {variant} automation")])

        logger.info(f"  Step 4B: Hash known as {variant} but no stored BT exists")
        validated = hash_result.get("validated", False)
        if not validated:
            logger.warning(
                f"  Step 4B: variant_cache entry for hash={skel_hash[:12]} is UNVALIDATED "
                f"(variant={variant}). claude-primary should mark_validated or "
                f"delete after observed-success on this screen."
            )
        if not request.screenshot_b64:
            return {
                "directive": "need_screenshot",
                "directive_id": _make_directive_id(),
                "reason": f"bt_build_for_{variant}",
            }
        # Pass the FULL variant string (e.g. "NAVIGATION_COURSE_OVERVIEW")
        # downstream, not just the master prefix. Was: variant.split("_")[0]
        # which dropped the subtype info needed by knowledge.json's subtype
        # matcher / operational_notes loader. Bug fix 2026-05-19.
        return _build_screen_directive(request, platform, tree, variant, skel_hash,
                                       course_id=cs.course_id)

    # ── Step 4.5: Signature-based fuzzy match (V19/V20 era — resurrected 2026-05-19) ──
    # When Step 4's exact skeleton_hash misses, try signature matching BEFORE
    # invoking the LLM classifier in Step 5. Signatures are stored per-platform
    # in /home/user/taey-ed-data/signatures/{platform}.json. Cold-start (< 2
    # signatures) falls back to exact-hash within the signature matcher. Once
    # 2+ signatures are learned, Jaccard similarity on discriminative markers
    # (signature - platform_common_chrome) at threshold 0.70 catches screens
    # with the same structural identity but mutated tree (different question
    # text, different timer values, different option strings).
    #
    # Empty signatures dir = always matched:False = pure passthrough, current
    # Step 5 behavior unchanged. Behavior becomes deterministic + zero-LLM-cost
    # only once learn_screen has populated entries (next commit seeds known
    # Khan screen types).
    from spark.tasks.screen_signatures import match_signature
    sig_result = match_signature(platform, tree)
    if sig_result.get("matched"):
        variant = sig_result["screen_type"]
        score = sig_result["match_score"]
        sig_hash = sig_result["sig_hash"]
        logger.info(
            f"  Step 4.5: SIGNATURE MATCH → variant={variant} "
            f"score={score:.2f} sig_hash={sig_hash} "
            f"validated={sig_result.get('validated', False)}"
        )
        # Populate the fast-path skeleton_hash cache so future encounters with
        # the same structural hash hit Step 4 directly (no Jaccard pass).
        register_hash(platform, skel_hash, variant)

        # Pending-validation notify hook (Jesse 2026-05-19): if the matched
        # signature is unvalidated, record so Step 2 pings claude-primary
        # after Mac's BT execution.
        if not sig_result.get("validated", False):
            try:
                from spark.tasks.validation_tracker import record_pending
                record_pending(
                    platform=platform,
                    skel_hash=skel_hash,
                    variant=variant,
                    source="signature",
                    tree=tree,
                    sig_hash=sig_hash,
                )
                logger.info(
                    f"  Step 4.5: recorded pending validation for unvalidated "
                    f"signature {sig_hash[:12]} → {variant}"
                )
            except Exception:
                logger.exception("Step 4.5 record_pending failed (non-fatal)")

        if sig_result.get("tree"):
            stored_bt = sig_result["tree"]
            bt_json = json.dumps(stored_bt, indent=2)
            logger.info(f"  Step 4.5: REUSING signature-matched BT for {variant}")
            logger.info(f"  Stored BT:\n{bt_json}")
            return _with_chat({
                "directive": "execute_tree",
                "directive_id": _make_directive_id(),
                "tree": stored_bt,
                "screen": variant,
                "skeleton_hash": skel_hash,
                "sig_hash": sig_hash,
                "extract": sig_result.get("extract"),
                "course_id": cs.course_id,
            }, platform, [build_status(f"Executing {variant} (signature match)")])

        # Signature matched but no stored BT — route to BT-build with the
        # known screen_type. Avoids the LLM classifier call in Step 5.
        logger.info(f"  Step 4.5: Signature matched as {variant} but no stored BT — building fresh")
        if not request.screenshot_b64:
            return {
                "directive": "need_screenshot",
                "directive_id": _make_directive_id(),
                "reason": f"bt_build_for_{variant}_via_signature",
            }
        return _build_screen_directive(
            request, platform, tree, variant, skel_hash,
            course_id=cs.course_id,
        )
    else:
        logger.info(f"  Step 4.5: no signature match — falling through to Step 5")

    # ── Step 5: No hash match — knowledge gate, then Flash classify ──
    logger.info(f"  Step 5: No hash match — checking knowledge gate")

    # Knowledge gate: no knowledge.json = research required first
    from spark.tasks.knowledge_loader import load_knowledge
    knowledge = load_knowledge(platform)
    if not knowledge:
        knowledge_path = f"spark/platforms/{platform}/knowledge.json"
        logger.warning(f"  Step 5: KNOWLEDGE GATE — no knowledge.json for {platform}")

        gate_flag = Path(f"/tmp/taey-ed-knowledge-gate-{platform}")
        if not gate_flag.exists():
            from spark.tasks.notify_tmux import notify_spark_claude
            prompt_template = "spark/platforms/DEEP_RESEARCH_PROMPT.md"
            platform_url = {
                "khan_academy": "https://www.khanacademy.org",
                "coursera": "https://www.coursera.org",
                "edx": "https://www.edx.org",
                "udemy": "https://www.udemy.com",
                "acellus": "https://www.acellus.com",
            }.get(platform, "UNKNOWN — look it up")
            notify_spark_claude(
                f"RESEARCH REQUIRED: No knowledge.json exists for platform '{platform}'.\n"
                f"You MUST use Perplexity Deep Research via taey's hands MCP tools.\n"
                f"DO NOT use WebSearch, WebFetch, or any other substitute.\n"
                f"DO NOT delegate this to a subagent — subagents cannot use MCP tools.\n\n"
                f"STEPS:\n"
                f"1. Read the prompt template: {prompt_template}\n"
                f"2. Fill placeholders: PLATFORM_NAME={platform}, PLATFORM_URL={platform_url}\n"
                f"3. Send filled prompt to Perplexity (Deep Research mode)\n"
                f"4. Extract JSON from response (Download, not Copy)\n"
                f"5. Save to: {knowledge_path}\n"
                f"6. Create dir: spark/platforms/{platform}/learned/\n"
                f"7. Platform will proceed automatically on next screen cycle."
            )
            gate_flag.write_text(str(time.time()))
            logger.info(f"  Step 5: Notified Spark Claude (first time for {platform})")
        else:
            logger.info(f"  Step 5: Already notified for {platform}, returning wait")

        return {
            "directive": "wait",
            "directive_id": _make_directive_id(),
            "seconds": 30.0,
            "reason": f"Waiting for knowledge.json research for {platform}",
        }

    # Knowledge gate passed
    gate_flag = Path(f"/tmp/taey-ed-knowledge-gate-{platform}")
    if gate_flag.exists():
        gate_flag.unlink()
        logger.info(f"  Step 5: Knowledge gate cleared for {platform}")

    # Hydration guard (claude-0 2026-06-13): Perseus/React widgets hydrate AFTER
    # the DOM mounts. A capture taken mid-hydration shows exercise content (the
    # question text + a graded "Check" button) but NO answer widgets yet — the
    # classifier then can't see the EXERCISE signal and flaps to UNKNOWN (which
    # cascades into the worker getting the generic guide and freelancing). When
    # the capture looks like an exercise mid-hydration, re-poll briefly instead
    # of classifying a widget-less snapshot. Bounded so a genuine no-widget
    # screen can't loop.
    if _looks_like_hydrating_exercise(tree):
        n = _bump_hydration_wait(platform)
        if n <= MAX_HYDRATION_WAITS:
            logger.info(
                f"  Step 5: capture looks mid-hydration (exercise content, no answer widgets) "
                f"— re-poll {n}/{MAX_HYDRATION_WAITS} before classifying"
            )
            return {
                "directive": "wait",
                "directive_id": _make_directive_id(),
                "seconds": 1.0,
                "reason": "awaiting_widget_hydration",
            }
        logger.warning(
            f"  Step 5: still no answer widgets after {MAX_HYDRATION_WAITS} re-polls "
            f"— proceeding to classify as-is"
        )
    else:
        _reset_hydration_wait(platform)

    # Step 5A: Need screenshot for Flash classification
    if not request.screenshot_b64:
        logger.info("  Step 5A: Requesting screenshot for Flash classification")
        return {
            "directive": "need_screenshot",
            "directive_id": _make_directive_id(),
            "reason": "classification_needed",
        }

    # Step 5B: Async screen classification via the worker queue.
    # /next_action must not block on a Claude subprocess in the HTTP hot path.
    from spark.tasks.classification_request import request_classification
    logger.info("  Step 5B: Queueing or reading async classification...")
    classification = request_classification(
        platform=platform,
        tree=tree,
        screenshot_b64=request.screenshot_b64,
        skel_hash=skel_hash,
        session_id=request.session_id,
    )
    if classification.get("status") != "complete":
        logger.info(
            "  Step 5B: classification pending for hash=%s (%s)",
            skel_hash[:12],
            classification.get("classification_id", "unknown"),
        )
        return {
            "directive": "wait",
            "directive_id": _make_directive_id(),
            "seconds": 1.0,
            "reason": "classification_pending",
        }
    screen_type = classification.get("screen_type", "UNKNOWN")
    # classify_screen returns the variant under 'platform_variant'; downstream
    # callers expect 'variant'. Fall back to master screen_type when no
    # variant was extracted (Pro is allowed to return only the master type
    # for genuinely ambiguous screens).
    variant = classification.get("platform_variant") or screen_type
    logger.info(
        f"  Step 5B: Classified type={screen_type} variant={variant} "
        f"note={classification.get('confidence_note', '')}"
    )

    # Register hash → variant for future exact lookups
    if variant != "UNKNOWN":
        register_hash(platform, skel_hash, variant)

    # LOOP GUARD: If Flash reclassifies as a variant that already failed, STOP
    if variant != "UNKNOWN" and _check_variant_failed(platform, variant):
        logger.error(
            f"  Step 5B: LOOP GUARD — Flash classified as {variant} which has failed "
            f"{MAX_VARIANT_FAILURES}+ times. STOPPING."
        )
        from spark.tasks.classify_screen import _describe_screen
        _reason = (f"Screen classified as '{variant}' which has repeatedly failed. "
                   f"Tell me what to do on this screen.")
        return _with_chat({
            "directive": "user_input_needed",
            "directive_id": _make_directive_id(),
            "reason": _reason,
            "screen_type": screen_type,
            "screen_description": _describe_screen(tree),
            "confidence_note": classification.get("confidence_note", ""),
        }, platform, [
            build_status(f"Screen '{variant}' keeps failing — need your help"),
            build_question(_reason),
        ])

    # Log fingerprint for V22 learning (non-blocking)
    try:
        from spark.tasks.skeleton import extract_content_fingerprint
        fingerprint = extract_content_fingerprint(tree)
        if fingerprint:
            _log_fingerprint(platform, variant, skel_hash, fingerprint)
    except Exception as e:
        logger.warning(f"  Step 5B: fingerprint logging failed (non-blocking): {e}")

    # Step 5C: Check variant BT cache
    if variant != "UNKNOWN":
        bt_entry = lookup_variant_bt(platform, variant)
        if bt_entry and bt_entry.get("behavior_tree"):
            stored_bt = bt_entry["behavior_tree"]
            bt_json = json.dumps(stored_bt, indent=2)
            logger.info(f"  Step 5C: REUSING variant BT for {variant}")
            logger.info(f"  Stored BT:\n{bt_json}")
            return _with_chat({
                "directive": "execute_tree",
                "directive_id": _make_directive_id(),
                "tree": stored_bt,
                "screen": variant,
                "skeleton_hash": skel_hash,
                "extract": bt_entry.get("extract"),
                "expected_next": bt_entry.get("expected_next", []),
                "course_id": cs.course_id,
            }, platform, [build_status(f"Executing {variant} automation")])

    # Step 5D: Worker consultation is the only BT build path now
    logger.info(f"  Step 5D: requesting worker consultation for {variant}")
    store_message(platform, build_status(f"Identified screen as {variant}"))

    if screen_type == "UNKNOWN":
        consult_result = request_consultation(
            platform=platform,
            tree=tree,
            screenshot_b64=request.screenshot_b64,
            context={
                "screen_type": "UNKNOWN",
                "screen_type_hint": "UNKNOWN",
                "course_id": cs.course_id,
            },
        )
        logger.info("  Step 5D: UNKNOWN classification escalated into consultation flow")
        return _consultation_or_wait(consult_result)

    return _build_screen_directive(
        request, platform, tree, variant, skel_hash, course_id=cs.course_id
    )
