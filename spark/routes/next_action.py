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
from spark.tasks.load_yaml import load_yaml
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

# ── Claude-primary platforms ──
# Platforms in this set bypass Flash classify + Pro BT-build entirely and route
# screens directly to the taey-ed tmux session (Spark Claude) for vision +
# BT-building. Reversible by removing the platform from the set.
CLAUDE_PRIMARY_PLATFORMS = {"khan_academy"}


def _maybe_claude_consult(
    request,
    platform: str,
    tree: dict,
    screen_type: str,
    user_guidance: str | None = None,
    failed_bt: dict | None = None,
    failed_bt_debug: str | None = None,
) -> dict | None:
    """
    If platform is Claude-primary, request a minimal consultation and return a
    directive (consulting / wait / need_screenshot). Returns None for
    Gemini-primary platforms so the caller proceeds normally.
    """
    if platform not in CLAUDE_PRIMARY_PLATFORMS:
        return None

    if not request.screenshot_b64:
        return {
            "directive": "need_screenshot",
            "directive_id": _make_directive_id(),
            "reason": f"claude_consultation_{screen_type}",
        }

    parts = []
    if user_guidance:
        parts.append(user_guidance)
    if failed_bt:
        parts.append(f"PREVIOUS BT FAILED:\n{json.dumps(failed_bt, indent=2)}")
    if failed_bt_debug:
        parts.append(f"BT debug log:\n{failed_bt_debug}")
    combined = "\n\n".join(parts) if parts else None

    from spark.tasks.consultation_request import request_minimal_consultation
    consult_result = request_minimal_consultation(
        platform=platform,
        tree=tree,
        screenshot_b64=request.screenshot_b64,
        screen_type=screen_type,
        user_guidance=combined,
        relevant_kb_chunks=request.relevant_kb_chunks,
    )
    return _consultation_or_wait(consult_result)


# ── Failure tracking (prevents infinite reclassify loops) ──
# Key: "{platform}:{variant}" → {"count": int, "last_failed": float}
# Reset when a DIFFERENT variant succeeds on the same platform.
_variant_failures: dict[str, dict] = {}
MAX_VARIANT_FAILURES = 2  # After 2 failures for same variant, STOP


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


def _store_and_return_bt(result: dict, platform: str, tree: dict, sig_hash: str,
                         course_id: str = "") -> dict:
    """Store a Gemini-built BT with the signature and return execute_tree directive."""
    variant_type = result.get("screen_type", "UNKNOWN")

    # V21: Store BT in variant cache (deterministic types only)
    from spark.tasks.variant_cache import store_variant_bt, is_non_deterministic, register_hash
    extract_config = result.get("extract")
    try:
        if not is_non_deterministic(platform, variant_type):
            store_variant_bt(platform, variant_type, result["tree"],
                             extract_config, result.get("expected_next"),
                             source="gemini_bt")
        # Register hash → variant if we have a skeleton hash
        if sig_hash:
            register_hash(platform, sig_hash, variant_type)
        logger.info(f"  Stored variant BT for {variant_type} (hash={sig_hash[:12] if sig_hash else 'none'})")
    except Exception as e:
        logger.warning(f"  Failed to store variant BT: {e}")

    bt_json = json.dumps(result["tree"], indent=2)
    logger.info(f"  Gemini BT for {variant_type}:\n{bt_json}")

    # Pre-record BT definition for learning (actual success/failure recorded in Step 2)
    learn_from_bt_result(
        platform=platform,
        screen_type=variant_type,
        bt_result={"success": True, "screen_type": variant_type,
                   "extract": result.get("extract", {})},
        bt_definition=result.get("tree", {}),
        skeleton_hash=sig_hash,
    )

    return _with_chat({
        "directive": "execute_tree",
        "directive_id": _make_directive_id(),
        "tree": result["tree"],
        "screen": variant_type,
        "skeleton_hash": sig_hash,
        "extract": result.get("extract"),
        "expected_next": result.get("expected_next", []),
        "course_id": course_id,
    }, platform, [build_status(f"Built new {variant_type} automation — executing")])


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
    return {
        "directive": "consulting",
        "directive_id": _make_directive_id(),
        "consultation_id": consult_result["consultation_id"],
        "poll_interval": 5.0,
    }


def _build_screen_directive(request, platform: str, tree: dict, screen_type: str, sig_hash: str,
                            user_guidance: str = None, course_id: str = "") -> dict:
    """
    Gemini 2.5 Pro builds a screen-specific BT by looking at the actual tree + screenshot.
    Templates are only a fallback when no screenshot is available.

    Flow:
    1. Has screenshot? → Gemini 2.5 Pro builds dynamic BT → store with signature
    2. No screenshot? → deterministic template as last resort
    3. Both fail? → user_input_needed
    """
    # Claude-primary platforms: bypass Gemini, route to consultation.
    _claude_directive = _maybe_claude_consult(
        request, platform, tree, screen_type, user_guidance=user_guidance,
    )
    if _claude_directive:
        return _claude_directive

    from spark.tasks.classify_screen import build_bt_from_tree

    # PRIMARY PATH: Gemini 2.5 Pro builds a BT specific to THIS screen
    if request.screenshot_b64:
        logger.info(f"  Building dynamic BT via Gemini 2.5 Pro for {screen_type}")
        result = build_bt_from_tree(
            tree=tree,
            screenshot_b64=request.screenshot_b64,
            platform=platform,
            screen_type=screen_type,
            user_guidance=user_guidance,
        )
        if result:
            return _store_and_return_bt(result, platform, tree, sig_hash, course_id=course_id)
        logger.error(f"  Gemini 2.5 Pro BT build failed for {screen_type} — no template fallback")

    # Gemini failed or no screenshot — escalate, don't use templates
    from spark.tasks.classify_screen import _describe_screen
    _reason = (f"Could not build behavior tree for '{screen_type}'. "
               f"Tell me what to do on this screen.")
    return _with_chat({
        "directive": "user_input_needed",
        "directive_id": _make_directive_id(),
        "reason": _reason,
        "screen_type": screen_type,
        "screen_description": _describe_screen(tree),
    }, platform, [
        build_status(f"Could not build automation for {screen_type}"),
        build_question(_reason),
    ])


def _validate_last_action(platform: str, config: dict, lr, current_tree: dict) -> dict:
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

    # Wrong answer detection
    wrong_answer = False
    if new_screen and new_screen == lr.screen:
        from spark.tasks.screen_type_util import get_master_category
        screen_master = get_master_category(new_screen)
        if screen_master == "EXERCISE":
            directive_hash = lr.directive_skeleton_hash or ""
            if after_hash and directive_hash and after_hash != directive_hash:
                logger.info(
                    f"Step 2: Same variant {new_screen} but different skeleton hash "
                    f"({directive_hash[:12]} → {after_hash[:12]}). "
                    f"Progress to next question, not wrong answer."
                )
            else:
                wrong_answer = True

    # Expected_next check (informational)
    expected_next = lr.directive_expected_next or []
    expected_match = None
    if expected_next and new_screen:
        expected_match = new_screen in expected_next

    validated = tree_changed and not wrong_answer

    return {
        "validated": validated,
        "screen_transitioned": tree_changed,
        "new_screen": new_screen,
        "wrong_answer": wrong_answer,
        "expected_next_match": expected_match,
        "after_skeleton_hash": after_hash,
        "reason": "validated" if validated else ("wrong_answer" if wrong_answer else "validation_failed"),
    }


@router.post("/next_action")
def next_action(request: NextActionRequest):
    """
    Directive Model: Mac sends state, Spark returns ONE directive.

    Replaces /match + /consult + /validate + /action_review from Mac's perspective.
    """
    platform = request.platform
    tree = request.tree
    cs = request.client_state or ClientState()
    lr = request.last_result

    config = load_yaml(platform)

    # Store incoming user chat message (proactive or response)
    if request.chat_message:
        store_message(platform, build_user_message(request.chat_message))
        logger.info(f"  Chat: stored user message: {request.chat_message[:80]}")

        # ── URGENT: User message = priority override ──
        # User messages are treated like error reports — full context, immediate action.
        # If user is telling us something, there's something wrong that needs addressing.
        if request.screenshot_b64:
            logger.info("  URGENT: User sent proactive message — overriding normal flow")
            # Build context from previous state
            guidance_parts = [f"USER MESSAGE (URGENT — address immediately): {request.chat_message}"]
            if lr:
                guidance_parts.append(f"Previous screen: {lr.screen or 'unknown'}")
                guidance_parts.append(f"Previous action: {lr.action or 'unknown'}")
                guidance_parts.append(f"Action succeeded: {lr.success}")
                if lr.bt_debug_tail:
                    guidance_parts.append(f"Last BT debug:\n{lr.bt_debug_tail}")
            user_guidance = "\n".join(guidance_parts)

            # Claude-primary platforms: bypass Gemini, route to consultation.
            _claude_directive = _maybe_claude_consult(
                request, platform, tree,
                screen_type=(lr.screen if lr else "UNKNOWN") or "UNKNOWN",
                user_guidance=user_guidance,
            )
            if _claude_directive:
                return _claude_directive

            # Classify current screen to give Gemini context
            from spark.tasks.classify_screen import classify_screen, build_bt_from_tree
            classification = classify_screen(
                tree=tree,
                screenshot_b64=request.screenshot_b64,
                platform=platform,
            )
            screen_type = classification.get("screen_type", "UNKNOWN")
            logger.info(f"  URGENT: Current screen classified as {screen_type}")

            result = build_bt_from_tree(
                tree=tree,
                screenshot_b64=request.screenshot_b64,
                platform=platform,
                screen_type=screen_type,
                user_guidance=user_guidance,
            )
            if result:
                from spark.tasks.skeleton import extract_skeleton, skeleton_hash as _urgent_hash
                sig_hash = _urgent_hash(extract_skeleton(tree))
                return _store_and_return_bt(result, platform, tree, sig_hash, course_id=cs.course_id)

            # Gemini couldn't build BT — ask user for more specific guidance
            logger.warning("  URGENT: Gemini couldn't build BT from user message")
            from spark.tasks.classify_screen import _describe_screen
            _reason = (f"I received your message but couldn't build an automation from it. "
                       f"Can you tell me more specifically what to do on this screen?")
            return _with_chat({
                "directive": "user_input_needed",
                "directive_id": _make_directive_id(),
                "reason": _reason,
                "screen_type": screen_type,
                "screen_description": _describe_screen(tree),
            }, platform, [
                build_status(f"Received your message — need more specific guidance"),
                build_question(_reason),
            ])

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
                from spark.tasks.classify_screen import _describe_screen
                logger.error(
                    f"Step 1: worker_fallback for {consultation_id} — "
                    f"surfacing as user_input_needed. reason={_wf_reason}"
                )
                _ui_reason = (
                    "I couldn't build a plan for this screen automatically. "
                    f"Tell me what to do here. (reason: {_wf_reason})"
                )
                return _with_chat({
                    "directive": "user_input_needed",
                    "directive_id": _make_directive_id(),
                    "reason": _ui_reason,
                    "screen_type": consult_status.get("screen_type", "UNKNOWN"),
                    "screen_description": _describe_screen(tree),
                    "consultation_id": consultation_id,
                }, platform, [
                    build_status("Worker couldn't build a plan — need your help"),
                    build_question(_ui_reason),
                ])
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
        elif consult_status.get("status") == "not_found":
            logger.warning(f"Consultation {consultation_id} not found, clearing and re-matching")
            # Fall through to Step 4 (match screen) instead of waiting forever
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
        vr = _validate_last_action(platform, config, lr, tree)

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

        elif vr["wrong_answer"]:
            # ONE TRY ONLY: Wrong answer means the action was wrong. STOP.
            logger.error(
                f"Step 2: WRONG ANSWER for {lr.screen} — "
                f"ONE TRY ONLY — stopping. Hash={lr.directive_skeleton_hash}"
            )
            if lr.directive_skeleton_hash:
                try:
                    from spark.tasks.variant_cache import delete_hash, invalidate_variant_bt
                    delete_hash(platform=platform, skel_hash=lr.directive_skeleton_hash)
                    if lr.screen:
                        invalidate_variant_bt(platform=platform, variant=lr.screen)
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
                build_status(f"Wrong answer detected on {lr.screen} — deleted old approach"),
                build_question(_reason),
            ])

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
        if not tree_hash_changed:
            logger.error(
                f"STUCK: {lr.screen} unchanged after action. "
                f"ONE TRY ONLY — stopping. Hash={lr.directive_skeleton_hash or 'none'}"
            )
            # Delete the failed hash mapping so it doesn't re-match next time
            if lr.directive_skeleton_hash:
                try:
                    from spark.tasks.variant_cache import delete_hash as _del_hash, invalidate_variant_bt as _inv_bt
                    _del_hash(platform=platform, skel_hash=lr.directive_skeleton_hash)
                    if lr.screen:
                        _inv_bt(platform=platform, variant=lr.screen)
                    logger.info(f"Step 2.5: Deleted failed hash {lr.directive_skeleton_hash[:12]}")
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
                build_status(f"Screen unchanged after action on {lr.screen} — need your help"),
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
        if lr.tree_hash_before != lr.tree_hash_after:
            # For VIDEO screens: tree hash ALWAYS changes during playback
            # (timestamps, progress bar). Check if video is actually done
            # by looking for video signals in the current tree.
            is_video = lr.screen and "VIDEO" in (lr.screen or "").upper()
            # TODO(V20): Consider using get_master_category() here for robustness.
            # Currently works because all video screen types contain "VIDEO".
            if is_video:
                from spark.tasks.prompt_codex import analyze_tree as _analyze
                current_tags = _analyze(tree)
                if "HAS_VIDEO" in current_tags:
                    logger.info(
                        f"Step 2.7: Video still playing (HAS_VIDEO in current tree). "
                        f"Continuing poll — NOT advancing."
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
                request, platform, tree, "TRANSITION", "",
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

            # Claude-primary platforms: route to consultation with the user guidance.
            _claude_directive = _maybe_claude_consult(
                request, platform, tree,
                screen_type=lr.screen or "UNKNOWN",
                user_guidance=lr.user_response,
            )
            if _claude_directive:
                return _claude_directive

            from spark.tasks.classify_screen import build_bt_from_tree
            result = build_bt_from_tree(
                tree=tree,
                screenshot_b64=request.screenshot_b64,
                platform=platform,
                screen_type=lr.screen or "UNKNOWN",
                user_guidance=lr.user_response,
            )
            if result:
                from spark.tasks.skeleton import extract_skeleton, skeleton_hash as _s3_hash
                sig_hash = _s3_hash(extract_skeleton(tree))
                return _store_and_return_bt(result, platform, tree, sig_hash, course_id=cs.course_id)

            # Gemini couldn't build BT from user guidance — fall back to consultation
            logger.warning("Step 3: Gemini couldn't build BT from user guidance, trying consultation")
            consult_result = request_consultation(
                platform=platform,
                tree=tree,
                screenshot_b64=request.screenshot_b64,
                context={"user_guidance": lr.user_response, "failed_screen": lr.screen},
            )
            return _consultation_or_wait(consult_result)

        # If Mac sent the failed BT, try ONE Gemini call with failure context
        if lr.failed_bt and request.screenshot_b64:
            logger.info(
                f"Step 3: BT failed for {lr.screen} — retrying with failure context"
            )

            # Claude-primary platforms: route the retry to consultation with
            # the failed BT + debug log as context.
            _claude_directive = _maybe_claude_consult(
                request, platform, tree,
                screen_type=lr.screen or "UNKNOWN",
                failed_bt=lr.failed_bt,
                failed_bt_debug=lr.bt_debug_tail,
            )
            if _claude_directive:
                return _claude_directive

            from spark.tasks.classify_screen import build_bt_from_tree
            result = build_bt_from_tree(
                tree=tree,
                screenshot_b64=request.screenshot_b64,
                platform=platform,
                screen_type=lr.screen or "UNKNOWN",
                failed_bt=lr.failed_bt,
                failed_bt_debug=lr.bt_debug_tail,
            )
            if result:
                from spark.tasks.skeleton import extract_skeleton, skeleton_hash as _s3r_hash
                retry_sig_hash = _s3r_hash(extract_skeleton(tree))
                return _store_and_return_bt(result, platform, tree, retry_sig_hash, course_id=cs.course_id)
            logger.error("Step 3: Gemini retry with failure context also failed — stopping")

        # ── Screen mismatch recovery ──
        # Record failure to prevent reclassify loops
        if lr.screen:
            _record_variant_failure(platform, lr.screen)

        # If the BT came from a hash match and it failed (including Gemini
        # retry), the hash mapping was likely wrong. Delete it.
        # BUT: check failure count BEFORE falling through to reclassify.
        if lr.directive_skeleton_hash:
            logger.info(
                f"Step 3: BT failed for hash-matched screen {lr.screen} "
                f"— deleting hash {lr.directive_skeleton_hash[:12]}"
            )
            try:
                from spark.tasks.variant_cache import delete_hash as _del_hash3, invalidate_variant_bt as _inv_bt3
                _del_hash3(platform=platform, skel_hash=lr.directive_skeleton_hash)
                if lr.screen:
                    _inv_bt3(platform=platform, variant=lr.screen)
            except Exception as e:
                logger.warning(f"Step 3: delete_hash failed: {e}")

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
        store_variant_bt, is_non_deterministic, mark_variant_validated,
        mark_hash_validated,
    )

    skel = extract_skeleton(tree)  # web_content_only=True by default
    skel_hash = _skel_hash(skel)
    logger.info(f"  Step 4: Exact hash lookup (hash={skel_hash[:12]})")

    hash_result = lookup_by_hash(platform, skel_hash)
    if hash_result:
        variant = hash_result["variant"]
        logger.info(f"  Step 4: Hash hit → variant={variant}")

        # For deterministic variants, try to reuse stored BT
        if not is_non_deterministic(platform, variant):
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

        # Non-deterministic variant (EXERCISE) or no stored BT — need Pro
        logger.info(f"  Step 4B: Hash known as {variant} but needs fresh BT")
        screen_type = variant.split("_")[0] if "_" in variant else variant
        if not request.screenshot_b64:
            return {
                "directive": "need_screenshot",
                "directive_id": _make_directive_id(),
                "reason": f"bt_build_for_{variant}",
            }
        return _build_screen_directive(request, platform, tree, screen_type, skel_hash,
                                       course_id=cs.course_id)

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

    # Claude-primary platforms: skip Flash + Pro entirely, route to consultation.
    # Step 4 (exact hash → reuse stored BT) still applies and stays free.
    _claude_directive = _maybe_claude_consult(
        request, platform, tree, screen_type="UNKNOWN",
    )
    if _claude_directive:
        logger.info(f"  Step 5: Claude-primary platform — routing to consultation")
        return _claude_directive

    # Step 5A: Need screenshot for Flash classification
    if not request.screenshot_b64:
        logger.info("  Step 5A: Requesting screenshot for Flash classification")
        return {
            "directive": "need_screenshot",
            "directive_id": _make_directive_id(),
            "reason": "classification_needed",
        }

    # Step 5B: Flash classification (screenshot only, ~$0.002)
    from spark.tasks.flash_classify import classify_screen_flash
    logger.info("  Step 5B: Flash classifying screen...")
    classification = classify_screen_flash(
        screenshot_b64=request.screenshot_b64,
        platform=platform,
    )
    variant = classification.get("variant", "UNKNOWN")
    screen_type = classification.get("screen_type", "UNKNOWN")
    logger.info(
        f"  Step 5B: Flash result: type={screen_type} variant={variant} "
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

    # Step 5C: Check variant BT cache (deterministic variants only)
    if variant != "UNKNOWN" and not is_non_deterministic(platform, variant):
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

    # Step 5D: Pro builds BT (new variant or non-deterministic)
    logger.info(f"  Step 5D: Pro building BT for {variant}")
    store_message(platform, build_status(f"Identified screen as {variant}"))

    if screen_type == "UNKNOWN":
        # UNKNOWN — try Pro BT builder, escalate if it fails
        logger.warning("  Step 5D: UNKNOWN screen — trying Gemini Pro BT builder")
        from spark.tasks.classify_screen import build_bt_from_tree, _describe_screen
        result = build_bt_from_tree(
            tree=tree,
            screenshot_b64=request.screenshot_b64,
            platform=platform,
            screen_type="UNKNOWN",
        )
        if result:
            result_variant = result.get("screen_type", "UNKNOWN")
            logger.info(f"  Step 5D: Pro built BT for UNKNOWN → {result_variant}")
            # Store variant BT if deterministic
            if not is_non_deterministic(platform, result_variant):
                store_variant_bt(platform, result_variant, result["tree"],
                                 result.get("extract"), result.get("expected_next"))
            register_hash(platform, skel_hash, result_variant)
            return _store_and_return_bt(result, platform, tree, skel_hash, course_id=cs.course_id)

        # Pro failed — escalate
        logger.warning("  Step 5D: Pro BT builder failed — requesting user input")
        _reason = ("Unknown screen type. Gemini could not build a behavior tree. "
                   "Tell me what to do on this screen.")
        return _with_chat({
            "directive": "user_input_needed",
            "directive_id": _make_directive_id(),
            "reason": _reason,
            "screen_type": "UNKNOWN",
            "screen_description": _describe_screen(tree),
            "confidence_note": classification.get("confidence_note", ""),
        }, platform, [
            build_status("Unknown screen — could not build automation"),
            build_question(_reason),
        ])

    # Known type — build BT via Pro, store for variant
    result = _build_screen_directive(request, platform, tree, screen_type, skel_hash,
                                     course_id=cs.course_id)

    # If Pro built a BT successfully, store it under the variant for reuse
    if result.get("directive") == "execute_tree" and not is_non_deterministic(platform, variant):
        bt = result.get("tree")
        if bt:
            store_variant_bt(platform, variant, bt,
                             result.get("extract"), result.get("expected_next"))

    return result
