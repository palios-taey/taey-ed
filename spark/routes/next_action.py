"""
/next_action — V8 Directive Model.

Mac sends state, Spark returns ONE directive. All intelligence server-side.
Mac is a dumb executor; Spark makes all decisions.

Flow:
  1. Active consultation? → wait or execute completed BT
  2. Validate previous action → stuck detection, wrong-answer detection
  3. match_screen() → vector-only (Weaviate ScreenEmbedding)
  4. Match found:
     A. Has BT → execute_tree
     B. No BT but screen_type known → consult with type context (skip classification)
  5. No match → classify via Gemini → store in Weaviate → consult for BT
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
  - Classifications stored in Weaviate immediately for future recognition
  - match_screen() returns skeleton/embedding even on no-match (avoid recompute)
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

logger = logging.getLogger(__name__)

router = APIRouter()


def _make_directive_id() -> str:
    return f"d-{uuid.uuid4().hex[:8]}"



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
        screen_upper = (new_screen or "").upper()
        if any(kw in screen_upper for kw in ["QUIZ", "ASSESSMENT", "EXERCISE"]):
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

                return {
                    "directive": "execute_tree",
                    "directive_id": _make_directive_id(),
                    "tree": tree_def,
                    "screen": consult_status.get("screen_type", "CONSULTATION"),
                    "extract": consult_status.get("extract"),
                    "course_id": consult_status.get("course_id", cs.course_id),
                    "lesson": "",
                    "expected_next": consult_status.get("expected_next", []),
                    "skeleton_hash": _consult_skeleton_hash,
                }
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
                    from spark.tasks.screen_memory import mark_validated
                    mark_validated(lr.directive_skeleton_hash, screen_type=lr.screen or "")
                    logger.info(
                        f"Step 2: Validated {lr.screen} "
                        f"(hash={lr.directive_skeleton_hash[:12]}, "
                        f"new_screen={vr.get('new_screen')})"
                    )
                except Exception as e:
                    logger.warning(f"Step 2: mark_validated failed (non-fatal): {e}")

        elif vr["wrong_answer"]:
            logger.warning(
                f"Step 2: WRONG ANSWER detected for {lr.screen} "
                f"(hash={lr.directive_skeleton_hash})"
            )
            if lr.directive_skeleton_hash:
                try:
                    from spark.tasks.screen_memory import mark_invalidated
                    mark_invalidated(lr.directive_skeleton_hash, screen_type=lr.screen or "")
                except Exception as e:
                    logger.warning(f"Step 2: mark_invalidated failed: {e}")

            if not request.screenshot_b64:
                return {
                    "directive": "need_screenshot",
                    "directive_id": _make_directive_id(),
                    "reason": "wrong_answer_needs_consultation",
                }
            consult_result = request_consultation(
                platform=platform,
                tree=tree,
                screenshot_b64=request.screenshot_b64,
                context={
                    "failure_reason": "wrong_answer_same_question",
                    "previous_screen": lr.screen,
                    "previous_action": lr.action,
                    "reconsultation": True,
                },
            )
            return _consultation_or_wait(consult_result)

        else:
            logger.warning(
                f"Step 2: Validation failed for {lr.screen}: {vr.get('reason')} "
                f"(new_screen={vr.get('new_screen')}, "
                f"expected_next_match={vr.get('expected_next_match')})"
            )

    # ── Step 2.5: Stuck detection ──
    logger.info("  Step 2.5: Checking for stuck screen...")
    if lr and lr.success and not lr.continue_loop and lr.screen:
        tree_hash_changed = lr.tree_hash_before != lr.tree_hash_after
        if not tree_hash_changed:
            logger.warning(f"STUCK: {lr.screen} unchanged after BT. Escalating.")
            if not request.screenshot_b64:
                return {
                    "directive": "need_screenshot",
                    "directive_id": _make_directive_id(),
                    "reason": "stuck_same_screen",
                }
            consult_result = request_consultation(
                platform=platform,
                tree=tree,
                screenshot_b64=request.screenshot_b64,
                context={
                    "stuck_screen": lr.screen,
                    "failure_reason": "BT executed but screen unchanged. BT is wrong.",
                    "reconsultation": True,
                },
            )
            return _consultation_or_wait(consult_result)

    # ── Step 2.7: Polling completion detection ──
    logger.info("  Step 2.7: Checking polling completion...")
    if lr and lr.continue_loop and lr.tree_hash_before and lr.tree_hash_after:
        if lr.tree_hash_before != lr.tree_hash_after:
            logger.info(
                f"Content completed: tree changed after {lr.action} "
                f"(screen={lr.screen}). Navigating forward."
            )
            return {
                "directive": "execute_tree",
                "directive_id": _make_directive_id(),
                "tree": {
                    "type": "sequence",
                    "children": [
                        {"type": "action", "action": "press_key", "params": {"key": "Escape"}},
                        {"type": "action", "action": "wait", "params": {"seconds": 2.0}},
                    ],
                },
                "screen": f"{lr.screen or 'CONTENT'}_COMPLETE",
            }

    # ── Step 3: Previous action failed? ──
    logger.info("  Step 3: Checking for previous failure...")
    if lr and lr.success is False:
        if lr.user_response:
            if not request.screenshot_b64:
                return {
                    "directive": "need_screenshot",
                    "directive_id": _make_directive_id(),
                    "reason": "failure_needs_consultation",
                }
            consult_result = request_consultation(
                platform=platform,
                tree=tree,
                screenshot_b64=request.screenshot_b64,
                context={"user_guidance": lr.user_response, "failed_screen": lr.screen},
            )
            return _consultation_or_wait(consult_result)

        if not request.screenshot_b64:
            return {
                "directive": "need_screenshot",
                "directive_id": _make_directive_id(),
                "reason": "failure_needs_consultation",
            }
        consult_result = request_consultation(
            platform=platform,
            tree=tree,
            screenshot_b64=request.screenshot_b64,
            context={
                "failed_screen": lr.screen,
                "failure_action": lr.action,
                "reconsultation": True,
            },
        )
        return _consultation_or_wait(consult_result)

    # ── Step 4: Match screen (Weaviate vector search) ──
    logger.info("  Step 4: Vector matching against Weaviate...")
    match_result = match_screen(tree, config)
    logger.info(
        f"    match_result: matched={match_result.get('matched')} "
        f"screen={match_result.get('screen', 'none')} "
        f"screen_type={match_result.get('screen_type', 'none')} "
        f"has_tree={'yes' if match_result.get('tree') else 'no'} "
        f"has_embedding={'yes' if match_result.get('embedding') else 'no'}"
    )

    if match_result.get("matched") and match_result.get("tree"):
        logger.info(f"  <<< RETURNING: execute_tree for {match_result.get('screen', 'UNKNOWN')}")
        return {
            "directive": "execute_tree",
            "directive_id": _make_directive_id(),
            "tree": match_result["tree"],
            "screen": match_result.get("screen", "UNKNOWN"),
            "extract": match_result.get("extract"),
            "course_id": match_result.get("course_id", cs.course_id),
            "lesson": match_result.get("lesson", ""),
            "expected_next": match_result.get("expected_next", []),
            "skeleton_hash": match_result.get("skeleton_hash", ""),
        }

    # ── Step 4B: Matched screen but no BT — previously classified ──
    if match_result.get("matched") and not match_result.get("tree"):
        known_type = match_result.get("screen_type", "")
        logger.info(
            f"  Step 4B: Screen recognized as {known_type} "
            f"(hash={match_result.get('skeleton_hash', '')[:12]}, "
            f"d={match_result.get('match_distance', 0):.4f}) — no BT stored"
        )
        # Screen type is known — consult with type context for BT generation
        if not request.screenshot_b64:
            return {
                "directive": "need_screenshot",
                "directive_id": _make_directive_id(),
                "reason": f"bt_generation_for_{known_type}",
            }
        consult_result = request_consultation(
            platform=platform,
            tree=tree,
            screenshot_b64=request.screenshot_b64,
            context={
                "screen_type": known_type,
                "reason": f"Screen classified as {known_type} but no behavior tree stored yet.",
            },
        )
        return _consultation_or_wait(consult_result)

    # ── Step 5: No match — classify and store ──
    logger.info("  Step 5: No Weaviate match — classifying and storing")

    # Step 5A: Need screenshot for classification
    if not request.screenshot_b64:
        logger.info("  Step 5A: Requesting screenshot for classification")
        return {
            "directive": "need_screenshot",
            "directive_id": _make_directive_id(),
            "reason": "classification_needed",
        }

    # Step 5A: Classify screen type via Gemini
    from spark.tasks.classify_screen import classify_screen
    classification = classify_screen(
        tree=tree,
        screenshot_b64=request.screenshot_b64,
        platform=platform,
    )
    screen_type = classification.get("screen_type", "UNKNOWN")
    logger.info(
        f"  Step 5A: Classification result: type={screen_type} "
        f"variant={classification.get('platform_variant', '')} "
        f"note={classification.get('confidence_note', '')}"
    )

    # ── Step 5B: Store classification in Weaviate ──
    # Store immediately so this screen is recognized on next encounter.
    # skeleton_hash and embedding come from match_screen() Step 4 (pre-computed).
    if screen_type != "UNKNOWN":
        skel_hash = match_result.get("skeleton_hash", "")
        embedding = match_result.get("embedding")
        skeleton_text = match_result.get("skeleton_text", "")

        if skel_hash and embedding:
            try:
                from spark.tasks.screen_memory import store_screen
                store_screen(
                    vector=embedding,
                    skeleton_hash=skel_hash,
                    platform=platform,
                    behavior_tree={},
                    skeleton_text=skeleton_text,
                    screen_type=screen_type,
                    validated=False,
                    source="classification",
                )
                logger.info(
                    f"  Step 5B: Stored {screen_type} in Weaviate "
                    f"(hash={skel_hash[:12]})"
                )
            except Exception as e:
                logger.error(f"  Step 5B: Failed to store classification: {e}")
        else:
            logger.warning(
                "  Step 5B: No skeleton/embedding from Step 4 — cannot store. "
                "This screen will need re-classification next time."
            )

    # ── Step 5C: Return directive based on classification ──
    if screen_type == "UNKNOWN":
        logger.warning("  Step 5C: UNKNOWN screen — escalating to consultation")
        consult_result = request_consultation(
            platform=platform,
            tree=tree,
            screenshot_b64=request.screenshot_b64,
            context={
                "classification_failed": True,
                "confidence_note": classification.get("confidence_note", ""),
            },
        )
        return _consultation_or_wait(consult_result)

    # Known type — consult with screen_type context for BT generation
    logger.info(
        f"  Step 5C: {screen_type} classified and stored — "
        f"consulting for BT generation"
    )
    consult_result = request_consultation(
        platform=platform,
        tree=tree,
        screenshot_b64=request.screenshot_b64,
        context={
            "screen_type": screen_type,
            "platform_variant": classification.get("platform_variant", ""),
            "confidence_note": classification.get("confidence_note", ""),
            "reason": f"First encounter of {screen_type} screen. Generate behavior tree.",
        },
    )
    return _consultation_or_wait(consult_result)
