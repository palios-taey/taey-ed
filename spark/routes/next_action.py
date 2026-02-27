"""
/next_action — V8 Directive Model.

Mac sends state, Spark returns ONE directive. All intelligence server-side.
Mac is a dumb executor; Spark makes all decisions.

Flow:
  1. Active consultation? → wait or execute completed BT
  2. Validate previous action → stuck detection, wrong-answer detection
  3. match_screen() → set-difference signature matching (JSON files)
  4. Match found:
     A. Has BT → execute_tree
     B. No BT but screen_type known → consult with type context (skip classification)
  5. No match → classify via Gemini → store signature → consult for BT
     UNKNOWN → consultation escalation

Screen types (6 universal + 1 escalation, IMS Caliper validated):
  NAVIGATION, VIDEO, ARTICLE, EXERCISE, TRANSITION, UNKNOWN

Bug fixes from V7:
  #8: Uses CURRENT tree hash for validation, not stale consultation hash
  #9: Single match_screen call (remove double-call)
  #10: Log + flag when after-tree matches nothing

Post-V8 fixes (2026-02-21):
  - Removed navigation auto-detect (link count heuristic broke Coursera)
  - Gemini classification replaces heuristic analyze_tree()
  - V17: Replaced Weaviate vector matching with set-difference signature matching
  - Signatures stored in /var/spark/taey-ed/signatures/{platform}.json
"""

import json
import logging
import uuid
from pathlib import Path

from fastapi import APIRouter

from spark.models import NextActionRequest, ClientState
from spark.tasks.load_yaml import load_yaml
from spark.tasks.match_screen import match_screen
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


def _with_chat(response: dict, platform: str, messages: list[dict]) -> dict:
    """Attach chat_messages to a response and persist them to Redis."""
    for msg in messages:
        store_message(platform, msg)
    response["chat_messages"] = messages
    return response



def _store_and_return_bt(result: dict, platform: str, tree: dict, sig_hash: str) -> dict:
    """Store a Gemini-built BT with the signature and return execute_tree directive."""
    variant_type = result.get("screen_type", "UNKNOWN")

    # Only store BT with signature for deterministic types (VIDEO, ARTICLE).
    # Non-deterministic types store signature for recognition but NOT the BT,
    # so Gemini rebuilds it fresh each time with current screen content.
    from spark.tasks.screen_type_util import is_deterministic
    bt_to_store = result["tree"] if is_deterministic(variant_type) else None

    extract_config = result.get("extract")
    try:
        from spark.tasks.screen_signatures import learn_screen
        stored_hash = learn_screen(
            platform=platform,
            tree=tree,
            screen_type=variant_type,
            behavior_tree=bt_to_store,
            extract=extract_config,
            source="gemini_bt",
        )
        logger.info(f"  Stored {'BT + ' if bt_to_store else ''}signature for {variant_type} (hash={stored_hash[:12]})")
    except Exception as e:
        logger.warning(f"  Failed to store Gemini BT: {e}")

    bt_json = json.dumps(result["tree"], indent=2)
    logger.info(f"  Gemini BT for {variant_type}:\n{bt_json}")

    return _with_chat({
        "directive": "execute_tree",
        "directive_id": _make_directive_id(),
        "tree": result["tree"],
        "screen": variant_type,
        "skeleton_hash": sig_hash,
        "extract": result.get("extract"),
        "expected_next": result.get("expected_next", []),
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
                            user_guidance: str = None) -> dict:
    """
    Gemini 2.5 Pro builds a screen-specific BT by looking at the actual tree + screenshot.
    Templates are only a fallback when no screenshot is available.

    Flow:
    1. Has screenshot? → Gemini 2.5 Pro builds dynamic BT → store with signature
    2. No screenshot? → deterministic template as last resort
    3. Both fail? → user_input_needed
    """
    from spark.tasks.classify_screen import get_click_target, build_bt_from_tree

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
            return _store_and_return_bt(result, platform, tree, sig_hash)
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

    # Re-match the after_tree to identify where we landed
    after_tree_to_match = lr.after_tree or current_tree
    match_result = match_screen(after_tree_to_match, config)
    new_screen = match_result.get("screen") if match_result.get("matched") else None

    # Bug #10: Log when after-tree matches nothing
    if not new_screen:
        logger.warning(
            f"Step 2: after_tree matches NO known screen "
            f"(hash changed {lr.tree_hash_before[:12]} → {lr.tree_hash_after[:12]})"
        )

    # Wrong answer detection
    wrong_answer = False
    if new_screen and new_screen == lr.screen:
        # V20: Use get_master_category() instead of hardcoded keyword list
        from spark.tasks.screen_type_util import get_master_category
        screen_master = get_master_category(new_screen)
        if screen_master == "EXERCISE":
            after_skeleton_hash = match_result.get("skeleton_hash", "")
            directive_hash = lr.directive_skeleton_hash or ""
            if after_skeleton_hash and directive_hash and after_skeleton_hash != directive_hash:
                logger.info(
                    f"Step 2: Same screen type {new_screen} but different skeleton hash "
                    f"({directive_hash[:12]} → {after_skeleton_hash[:12]}). "
                    f"Progress to next question, not wrong answer."
                )
            else:
                wrong_answer = True

    # Expected_next check (informational)
    expected_next = lr.directive_expected_next or []
    expected_match = None
    if expected_next and new_screen:
        expected_match = new_screen in expected_next

    validated = tree_changed and (new_screen is not None) and not wrong_answer

    return {
        "validated": validated,
        "screen_transitioned": tree_changed,
        "new_screen": new_screen,
        "wrong_answer": wrong_answer,
        "expected_next_match": expected_match,
        "reason": "validated" if validated else ("wrong_answer" if wrong_answer else "validation_failed"),
    }


@router.post("/next_action")
def next_action(request: NextActionRequest):
    """
    V8 Directive Model: Mac sends state, Spark returns ONE directive.

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
                sig_hash = ""
                try:
                    _match = match_screen(tree, config)
                    sig_hash = _match.get("sig_hash", "")
                except Exception:
                    pass
                return _store_and_return_bt(result, platform, tree, sig_hash)

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
                    from spark.tasks.screen_signatures import mark_validated
                    mark_validated(platform=platform, sig_hash=lr.directive_skeleton_hash)
                    logger.info(
                        f"Step 2: Validated {lr.screen} "
                        f"(hash={lr.directive_skeleton_hash[:12]}, "
                        f"new_screen={vr.get('new_screen')})"
                    )
                except Exception as e:
                    logger.warning(f"Step 2: mark_validated failed (non-fatal): {e}")

        elif vr["wrong_answer"]:
            # ONE TRY ONLY: Wrong answer means the action was wrong. STOP.
            logger.error(
                f"Step 2: WRONG ANSWER for {lr.screen} — "
                f"ONE TRY ONLY — stopping. Hash={lr.directive_skeleton_hash}"
            )
            if lr.directive_skeleton_hash:
                try:
                    from spark.tasks.screen_signatures import delete_screen
                    delete_screen(platform=platform, sig_hash=lr.directive_skeleton_hash)
                except Exception as e:
                    logger.warning(f"Step 2: delete_screen failed: {e}")
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
            # Delete the failed entry so it doesn't re-match next time
            if lr.directive_skeleton_hash:
                try:
                    from spark.tasks.screen_signatures import delete_screen as _del_screen
                    _del_screen(platform=platform, sig_hash=lr.directive_skeleton_hash)
                    logger.info(f"Step 2.5: Deleted failed screen entry {lr.directive_skeleton_hash[:12]}")
                except Exception as e:
                    logger.warning(f"Step 2.5: delete_screen failed: {e}")
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
                user_guidance="Content just completed. Find and click the completion/mark-complete button if present, then the advance/next button. Look at the tree for actual button names.",
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
            from spark.tasks.classify_screen import build_bt_from_tree
            result = build_bt_from_tree(
                tree=tree,
                screenshot_b64=request.screenshot_b64,
                platform=platform,
                screen_type=lr.screen or "UNKNOWN",
                user_guidance=lr.user_response,
            )
            if result:
                # Get signature hash for storage
                sig_hash = ""
                try:
                    match_result = match_screen(tree, config)
                    sig_hash = match_result.get("sig_hash", "")
                except Exception:
                    pass
                return _store_and_return_bt(result, platform, tree, sig_hash)

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
                retry_sig_hash = ""
                try:
                    retry_match = match_screen(tree, config)
                    retry_sig_hash = retry_match.get("sig_hash", "")
                except Exception:
                    pass
                return _store_and_return_bt(result, platform, tree, retry_sig_hash)
            logger.error("Step 3: Gemini retry with failure context also failed — stopping")

        # ── Screen mismatch recovery ──
        # If the BT came from a signature match and it failed (including Gemini
        # retry), the signature was likely a false match. Delete it and let
        # Steps 4/5 re-classify the screen from scratch.
        if lr.directive_skeleton_hash:
            logger.info(
                f"Step 3: BT failed for signature-matched screen {lr.screen} "
                f"— deleting signature {lr.directive_skeleton_hash[:12]} and re-classifying"
            )
            try:
                from spark.tasks.screen_signatures import delete_screen
                delete_screen(platform=platform, sig_hash=lr.directive_skeleton_hash)
            except Exception as e:
                logger.warning(f"Step 3: delete_screen failed: {e}")
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

    # ── Step 4: Match screen (set-difference signatures) ──
    logger.info("  Step 4: Signature matching...")
    match_result = match_screen(tree, config)
    logger.info(
        f"    match_result: matched={match_result.get('matched')} "
        f"screen={match_result.get('screen', 'none')} "
        f"screen_type={match_result.get('screen_type', 'none')} "
        f"has_tree={'yes' if match_result.get('tree') else 'no'} "
        f"score={match_result.get('match_score', 'n/a')}"
    )

    # Step 4: Signature matched — check for stored BT first, then templates
    if match_result.get("matched"):
        known_type = match_result.get("screen_type", "")
        sig_hash = match_result.get("sig_hash", "")
        stored_bt = match_result.get("tree")

        # Only reuse stored BTs for deterministic screen types (VIDEO, ARTICLE).
        # All other types get fresh Gemini analysis since content changes.
        from spark.tasks.screen_type_util import is_deterministic
        if stored_bt and isinstance(stored_bt, dict) and stored_bt.get("type") and is_deterministic(known_type):
            logger.info(
                f"  Step 4: REUSING deterministic BT for {known_type} "
                f"(hash={sig_hash[:12]}, score={match_result.get('match_score', 0):.2f})"
            )
            bt_json = json.dumps(stored_bt, indent=2)
            logger.info(f"  Stored BT:\n{bt_json}")
            return _with_chat({
                "directive": "execute_tree",
                "directive_id": _make_directive_id(),
                "tree": stored_bt,
                "screen": known_type,
                "skeleton_hash": sig_hash,
                "extract": match_result.get("extract") or _get_extract_for_type(
                    known_type, tree=tree, screenshot_b64=request.screenshot_b64, platform=platform),
                "expected_next": [],
            }, platform, [build_status(f"Executing {known_type} automation")])

        # Non-deterministic type or no stored BT — build fresh via Gemini
        logger.info(
            f"  Step 4B: Screen recognized as {known_type} "
            f"(hash={sig_hash[:12]}, "
            f"score={match_result.get('match_score', 0):.2f}) — building fresh BT via Gemini"
        )
        if not request.screenshot_b64:
            return {
                "directive": "need_screenshot",
                "directive_id": _make_directive_id(),
                "reason": f"click_target_for_{known_type}",
            }
        return _build_screen_directive(request, platform, tree, known_type, sig_hash)

    # ── Step 5: No match — classify via Gemini and store ──
    # V20: Always use Gemini for classification. No structural shortcutting.
    # Per REQUIREMENTS.md: "Gemini sees the screen. Gemini decides."
    logger.info("  Step 5: No signature match — classifying via Gemini")

    # Step 5A: Need screenshot for classification + BT building
    if not request.screenshot_b64:
        logger.info("  Step 5A: Requesting screenshot for classification/BT building")
        return {
            "directive": "need_screenshot",
            "directive_id": _make_directive_id(),
            "reason": "classification_needed",
        }

    # V20: Always classify via Gemini — no structural shortcuts
    from spark.tasks.classify_screen import classify_screen
    classification = classify_screen(
        tree=tree,
        screenshot_b64=request.screenshot_b64,
        platform=platform,
    )
    screen_type = classification.get("screen_type", "UNKNOWN")
    logger.info(
        f"  Step 5A: Gemini classification result: type={screen_type} "
        f"variant={classification.get('platform_variant', '')} "
        f"note={classification.get('confidence_note', '')}"
    )

    # ── Step 5B: Store classification as signature ──
    # Store immediately so this screen is recognized on next encounter.
    if screen_type != "UNKNOWN":
        try:
            from spark.tasks.screen_signatures import learn_screen
            sig_hash = learn_screen(
                platform=platform,
                tree=tree,
                screen_type=screen_type,
                source="classification",
            )
            logger.info(f"  Step 5B: Stored {screen_type} signature ({sig_hash})")
        except Exception as e:
            logger.error(f"  Step 5B: Failed to store classification: {e}")

    # ── Step 5C: Return directive based on classification ──
    if screen_type == "UNKNOWN":
        logger.warning("  Step 5C: UNKNOWN screen — trying Gemini BT builder first")
        from spark.tasks.classify_screen import build_bt_from_tree, _describe_screen
        result = build_bt_from_tree(
            tree=tree,
            screenshot_b64=request.screenshot_b64,
            platform=platform,
            screen_type="UNKNOWN",
        )
        if result:
            logger.info(f"  Step 5C: Gemini built BT for UNKNOWN → {result.get('screen_type')}")
            # Store signature with the Gemini-determined type
            try:
                from spark.tasks.screen_signatures import learn_screen
                sig_hash = learn_screen(
                    platform=platform,
                    tree=tree,
                    screen_type=result["screen_type"],
                    behavior_tree=result["tree"],
                    extract=result.get("extract"),
                    source="gemini_bt",
                )
            except Exception as e:
                sig_hash = ""
                logger.warning(f"  Step 5C: Signature storage failed: {e}")
            return _store_and_return_bt(result, platform, tree, sig_hash)

        # Gemini BT builder failed — describe screen and ask for user input
        logger.warning("  Step 5C: Gemini BT builder failed — requesting user input")
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

    # Known type — identified via classification, now build BT
    logger.info(f"  Step 5C: {screen_type} — building BT")
    store_message(platform, build_status(f"Identified screen as {screen_type}"))
    return _build_screen_directive(request, platform, tree, screen_type, sig_hash if screen_type != "UNKNOWN" else "")
