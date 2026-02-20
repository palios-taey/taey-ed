"""
/next_action — V8 Directive Model.

Mac sends state, Spark returns ONE directive. All intelligence server-side.
Mac is a dumb executor; Spark makes all decisions.

Flow:
  1. Active consultation? → wait or execute completed BT
  2. Validate previous action → stuck detection, wrong-answer detection
  3. match_screen() → vector-only (Weaviate ScreenEmbedding)
  4. No match + many links → NAVIGATION_AUTO (LLM picks first incomplete)
  5. No match + few links → consultation (screenshot needed)

Bug fixes from V7:
  #8: Uses CURRENT tree hash for validation, not stale consultation hash
  #9: Single match_screen call (remove double-call)
  #10: Log + flag when after-tree matches nothing

Post-V8 fixes (2026-02-20):
  - Step 1: Handle "not_found" consultations (no infinite wait)
  - Step 2.5: Bypass stuck escalation for navigation screens (≥5 links)
  - Step 5: Navigation auto-detect with LLM intelligence
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


def _count_role(tree: dict, role: str) -> int:
    """Count elements with a given role in the accessibility tree."""
    count = 0
    stack = [tree]
    while stack:
        node = stack.pop()
        if node.get("role") == role:
            count += 1
        children = node.get("children")
        if children:
            stack.extend(children)
    return count


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

    # ── Step 1: Active consultation? Check if done ──
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
    if lr and lr.success and not lr.continue_loop and lr.screen:
        tree_hash_changed = lr.tree_hash_before != lr.tree_hash_after
        if not tree_hash_changed:
            # Before escalating, check if current screen is a navigation page.
            # Navigation screens don't need consultation — just send to LLM.
            link_count = _count_role(tree, "AXLink")
            if link_count >= 5:
                logger.info(
                    f"STUCK on {lr.screen} but current screen has {link_count} links — "
                    f"treating as navigation screen instead of escalating."
                )
                # Fall through to Step 4/5 instead of creating consultation
            else:
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

    # ── Step 4: Match screen ──
    match_result = match_screen(tree, config)

    if match_result.get("matched") and match_result.get("tree"):
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

    # ── Step 5: Navigation detection ──
    # If the screen has many links, it's a navigation screen.
    # Skip consultation — just return the navigate BT directly.
    link_count = _count_role(tree, "AXLink")
    if link_count >= 5:
        logger.info(
            f"Navigation screen detected ({link_count} links). "
            f"Returning navigate BT directly — no consultation needed."
        )
        return {
            "directive": "execute_tree",
            "directive_id": _make_directive_id(),
            "tree": {
                "type": "sequence",
                "children": [
                    {
                        "type": "action",
                        "action": "find_all",
                        "params": {"role": "AXLink"},
                        "store": "all_links",
                    },
                    {
                        "type": "action",
                        "action": "send_to_llm",
                        "params": {
                            "question_type": "navigate",
                            "question": (
                                "From the list of links, find the first incomplete content item.\n"
                                "Rules:\n"
                                "1. Skip items starting with 'completed' (already done)\n"
                                "2. Skip navigation links (Skip to main content, Search, Donate, Log in, Sign up, user menu)\n"
                                "3. If 'Try again' appears, return it (first priority)\n"
                                "4. If 'Practice' appears, return it (second priority)\n"
                                "5. Otherwise return the first Video or Article link NOT marked completed\n"
                                "6. If none of the above, return the first Unit link\n"
                                "\n"
                                "Return ONLY the exact description text from one item in the list. "
                                "Do not add any extra words, prefixes, or suffixes."
                            ),
                            "items": "$all_links",
                        },
                        "store": "nav_result",
                    },
                    {
                        "type": "action",
                        "action": "find_and_click",
                        "params": {
                            "target": "$nav_result.answer",
                            "role": "AXLink",
                            "strategy": "mouse_click",
                            "match_mode": "contains",
                            "post_delay": 3.0,
                        },
                    },
                ],
            },
            "screen": "NAVIGATION_AUTO",
        }

    # Not matched, not navigation — needs consultation
    if not request.screenshot_b64:
        return {
            "directive": "need_screenshot",
            "directive_id": _make_directive_id(),
            "reason": "consultation_needed",
        }
    consult_result = request_consultation(
        platform=platform,
        tree=tree,
        screenshot_b64=request.screenshot_b64,
        context={},
    )
    return _consultation_or_wait(consult_result)
