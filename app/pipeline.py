"""
Pipeline V8 - Directive-based execution loop.

Mac is a dumb executor. Spark makes ALL decisions via /next_action.
No escalation logic. No retry logic. No validation logic. No consultation
management. Just: capture -> ask -> do -> report.

Directives from Spark:
  execute_tree    - Execute behavior tree, report result
  wait            - Sleep and re-poll (consulting, page loading)
  need_screenshot - Capture screenshot, send immediately
  consulting      - Consultation started, poll via next call
  user_input_needed - Show in chat panel, collect user text
  stop            - Pipeline done
"""

import logging
import time
import threading
import uuid

from app.tasks.capture_tree import capture_tree
from app.tasks.capture_macapptree import capture_macapptree
from app.tasks.call_spark import call_spark
from app.tasks.compute_tree_hash import compute_tree_hash
from app.tasks.click_element import StaleElementError

# Content extraction
from app.tasks.handle_extraction import handle_extraction

# Behavior tree engine
from app.tasks.behavior_tree import execute_tree

# Checkpointing for crash recovery
from app.tasks.checkpoint import save_checkpoint, load_checkpoint, clear_checkpoint

# Browser URL verification (V1)
from app.tasks.browser_url import verify_browser_url

logger = logging.getLogger("taey-ed")


def _strip_tree_for_validation(tree: dict) -> dict:
    """
    Strip position/size/bbox/element_id from tree for validation payload.

    Spark only needs the structural skeleton (role, name, value, children)
    to re-match and validate. Visual layout data bloats the HTTP payload
    and is irrelevant for skeleton extraction.
    """
    STRIP_KEYS = {"position", "size", "visible_bbox", "element_id"}

    def _strip(node: dict) -> dict:
        stripped = {k: v for k, v in node.items() if k not in STRIP_KEYS and k != "children"}
        children = node.get("children")
        if children:
            stripped["children"] = [_strip(c) for c in children]
        return stripped

    return _strip(tree)


# =============================================================================
# Single-shot: one /next_action cycle (useful for testing)
# =============================================================================

def run_one_screen(
    platform: str,
    app_name: str,
    course_id: str = "unknown",
    platform_type: str = "app",
) -> dict:
    """
    Run one /next_action cycle for testing.

    Captures tree, calls /next_action, executes if directive is execute_tree.
    Does NOT loop — returns after first directive.
    """
    # URL check
    if platform_type == "browser":
        url_check = verify_browser_url(app_name, platform)
        if not url_check["ok"]:
            logger.warning(f"URL check: {url_check['message']}")

    tree = capture_tree(app_name)
    tree_hash = compute_tree_hash(tree)

    payload = {
        "session_id": str(uuid.uuid4()),
        "platform": platform,
        "tree": tree,
        "screenshot_b64": None,
        "client_state": {
            "screens_completed": 0,
            "last_tree_hash": tree_hash,
            "course_id": course_id,
            "platform_type": platform_type,
        },
        "last_result": None,
    }

    directive = call_spark("/next_action", payload)
    dtype = directive.get("directive", "stop")
    logger.info(f"Directive: {dtype} (screen={directive.get('screen', '?')})")

    if dtype == "execute_tree":
        tree_def = directive.get("tree")
        if not tree_def:
            return {"success": False, "reason": "execute_tree_no_tree", "directive": directive}

        # Extract before action
        extract_config = directive.get("extract")
        if extract_config:
            macapptree = capture_macapptree(app_name)
            handle_extraction(
                platform=platform,
                course_id=directive.get("course_id", course_id),
                tree=tree,
                screenshot_b64=macapptree.get("screenshot_b64", ""),
                extract_config=extract_config,
                screen_type=directive.get("screen", "UNKNOWN"),
                lesson=directive.get("lesson", ""),
            )

        bt_result = execute_tree(
            tree_definition=tree_def,
            app_name=app_name,
            platform=platform,
            course_id=directive.get("course_id", course_id),
            extract_config=extract_config,
        )

        return {
            "success": bt_result.get("success", False),
            "screen": directive.get("screen", "UNKNOWN"),
            "action": bt_result.get("action", "behavior_tree"),
            "continue_loop": bt_result.get("continue_loop", False),
            "tree_hash": tree_hash,
        }

    elif dtype == "stop":
        return {
            "success": directive.get("success", False),
            "reason": directive.get("reason", "stop"),
            "message": directive.get("message", ""),
            "tree_hash": tree_hash,
        }

    else:
        # wait, need_screenshot, consulting, user_input_needed
        return {
            "success": True,
            "reason": f"directive:{dtype}",
            "directive": directive,
            "tree_hash": tree_hash,
        }


# =============================================================================
# Continuous loop: capture -> ask -> do -> report
# =============================================================================

def run_continuous(
    platform: str,
    app_name: str,
    stop_event: threading.Event = None,
    inter_screen_delay: float = 2.0,
    course_id: str = "unknown",
    platform_type: str = "app",
    max_screens: int = 0,
    screen_callback=None,
    user_escalation_callback=None,
    chat_message_callback=None,
    user_input_callback=None,
    pending_chat_messages=None,
) -> dict:
    """
    Run automation continuously via /next_action directive loop.

    Mac sends state, Spark returns one directive. Mac executes it and reports.
    All decision logic (matching, consultation, escalation, validation) is
    on Spark. Mac is a dumb executor.

    Args:
        platform: Platform key (e.g., "khan_academy")
        app_name: macOS process name (e.g., "Google Chrome")
        stop_event: User stop button
        inter_screen_delay: Seconds between screen cycles
        course_id: Course identifier from UI
        platform_type: "app" or "browser"
        max_screens: Stop after N screens (0 = unlimited)
        screen_callback: Called after each screen completes (for UI progress)
        user_escalation_callback: LEGACY — Called when Spark needs user input (modal dialog)
        chat_message_callback: Called with list of chat messages from Spark
        user_input_callback: Called with directive when user input needed (chat-based)
        pending_chat_messages: Object with get_pending_chat_message() method
    """
    if stop_event is None:
        stop_event = threading.Event()

    session_id = str(uuid.uuid4())
    screens_completed = 0
    consecutive_errors = 0
    last_result = None
    pending_screenshot = None
    active_consultation_id = None
    last_directive_type = None  # Track for log dedup
    # Checkpoint recovery
    checkpoint = load_checkpoint(platform, course_id, app_name)
    if checkpoint:
        screens_completed = checkpoint["screens_completed"]
        logger.info(
            f"RESUMING from checkpoint: {screens_completed} screens "
            f"(last={checkpoint['last_screen']})"
        )

    # Browser URL check (once at start)
    if platform_type == "browser":
        url_check = verify_browser_url(app_name, platform)
        if not url_check["ok"]:
            logger.warning(f"URL check: {url_check['message']}")

    logger.info(f"=== V8 Continuous mode: {platform} ({app_name}) session={session_id} ===")

    while not stop_event.is_set():
        try:
            # ── Capture current screen ──
            tree = capture_tree(app_name)
            tree_hash = compute_tree_hash(tree)

            # ── Build request ──
            payload = {
                "session_id": session_id,
                "platform": platform,
                "tree": tree,
                "screenshot_b64": pending_screenshot,
                "client_state": {
                    "screens_completed": screens_completed,
                    "last_tree_hash": tree_hash,
                    "course_id": course_id,
                    "platform_type": platform_type,
                    "active_consultation_id": active_consultation_id,
                },
                "last_result": last_result,
            }
            pending_screenshot = None  # Clear after sending

            # Include pending chat message from user (proactive)
            if pending_chat_messages:
                try:
                    chat_msg = pending_chat_messages.get_pending_chat_message()
                    if chat_msg:
                        payload["chat_message"] = chat_msg
                except Exception:
                    pass

            # ── Ask Spark what to do ──
            directive = call_spark("/next_action", payload)
            dtype = directive.get("directive", "stop")
            directive_id = directive.get("directive_id", "")
            prev_directive_type = last_directive_type
            last_directive_type = dtype
            consecutive_errors = 0  # Reset on successful /next_action call

            # Deliver chat messages from Spark response
            chat_messages = directive.get("chat_messages")
            if chat_messages and chat_message_callback:
                try:
                    chat_message_callback(chat_messages)
                except Exception as e:
                    logger.warning(f"Chat message delivery failed: {e}")

            # ══════════════════════════════════════════════════════════
            # EXECUTE_TREE: Match found, execute behavior tree
            # ══════════════════════════════════════════════════════════
            if dtype == "execute_tree":
                active_consultation_id = None
                screen = directive.get("screen", "UNKNOWN")
                logger.info(f"Execute tree for {screen}")

                tree_def = directive.get("tree")
                if not tree_def:
                    logger.error(f"execute_tree directive has no tree for {screen}")
                    last_result = {
                        "directive_id": directive_id,
                        "success": False,
                        "action": "no_tree_in_directive",
                        "screen": screen,
                        "tree_hash_before": tree_hash,
                        "tree_hash_after": tree_hash,
                    }
                    time.sleep(inter_screen_delay)
                    continue

                # Extract content before action (if configured)
                extract_config = directive.get("extract")
                if extract_config:
                    logger.info("Extracting content before action...")
                    try:
                        macapptree = capture_macapptree(app_name)
                        handle_extraction(
                            platform=platform,
                            course_id=directive.get("course_id", course_id),
                            tree=tree,
                            screenshot_b64=macapptree.get("screenshot_b64", ""),
                            extract_config=extract_config,
                            screen_type=screen,
                            lesson=directive.get("lesson", ""),
                        )
                    except Exception as e:
                        logger.error(f"Extraction failed (continuing): {e}")

                # Execute behavior tree
                before_hash = tree_hash
                bt_result = execute_tree(
                    tree_definition=tree_def,
                    app_name=app_name,
                    platform=platform,
                    course_id=directive.get("course_id", course_id),
                    extract_config=extract_config,
                )

                # Capture after-state — wait for page to actually change.
                # Many clicks trigger async page loads (0.5-2s). Capturing
                # immediately sees the OLD page, causing false "same_screen"
                # which cascades into false wrong_answer detection.
                if bt_result.get("success") and not bt_result.get("continue_loop"):
                    PAGE_CHANGE_TIMEOUT = 5.0
                    PAGE_CHANGE_POLL = 0.3
                    waited = 0.0
                    after_tree = capture_tree(app_name)
                    after_hash = compute_tree_hash(after_tree)
                    while after_hash == before_hash and waited < PAGE_CHANGE_TIMEOUT:
                        time.sleep(PAGE_CHANGE_POLL)
                        waited += PAGE_CHANGE_POLL
                        after_tree = capture_tree(app_name)
                        after_hash = compute_tree_hash(after_tree)
                    if waited > 0 and after_hash != before_hash:
                        logger.info(f"Page changed after {waited:.1f}s")
                else:
                    after_tree = capture_tree(app_name)
                    after_hash = compute_tree_hash(after_tree)

                # If BT reported success but tree didn't change after waiting,
                # report as failure so Spark gets an honest signal.
                if bt_result.get('success') and not bt_result.get('continue_loop') and before_hash == after_hash:
                    logger.warning(
                        f'BT reported success but tree unchanged after {PAGE_CHANGE_TIMEOUT}s — marking failure'
                    )
                    bt_result['success'] = False
                    bt_result['action'] = f"{bt_result.get('action', 'behavior_tree')} (tree_unchanged)"

                # Build result for next call
                last_result = {
                    "directive_id": directive_id,
                    "success": bt_result.get("success", False),
                    "action": bt_result.get("action", "behavior_tree"),
                    "screen": screen,
                    "tree_hash_before": before_hash,
                    "tree_hash_after": after_hash,
                    "continue_loop": bt_result.get("continue_loop", False),
                }

                # Send failed BT so Spark/Gemini knows what was tried
                if not bt_result.get("success", False):
                    last_result["failed_bt"] = tree_def

                # Validation chain: ALWAYS send after_tree so Spark can analyze
                # what screen we landed on — especially important when BT fails,
                # so Spark can diagnose the failure and consult accurately.
                last_result["after_tree"] = _strip_tree_for_validation(after_tree)
                # Send BT debug log tail for Spark-side diagnostics
                try:
                    with open("/tmp/behavior_tree_debug.log") as _btf:
                        _bt_lines = _btf.readlines()
                        last_result["bt_debug_tail"] = "".join(_bt_lines[-20:])
                except Exception:
                    pass
                # Always send skeleton_hash so Spark can invalidate bad signatures on failure
                last_result["directive_skeleton_hash"] = directive.get("skeleton_hash", "")
                if bt_result.get("success") and not bt_result.get("continue_loop"):
                    last_result["directive_expected_next"] = directive.get("expected_next", [])

                # Screen completed: success AND not a polling action
                if bt_result.get("success") and not bt_result.get("continue_loop"):
                    screens_completed += 1
                    logger.info(f"Screen completed: {screens_completed} ({screen})")

                    save_checkpoint(
                        platform, course_id, app_name, screens_completed,
                        last_screen=screen,
                        last_action=bt_result.get("action", ""),
                    )

                    if screen_callback:
                        try:
                            screen_callback(screens_completed, max_screens)
                        except Exception:
                            pass

                    if max_screens > 0 and screens_completed >= max_screens:
                        logger.info(f"Reached max_screens ({max_screens})")
                        clear_checkpoint(platform, course_id, app_name)
                        return {
                            "success": True,
                            "reason": "max_screens_reached",
                            "screens_completed": screens_completed,
                        }

                time.sleep(inter_screen_delay)

            # ══════════════════════════════════════════════════════════
            # WAIT: Spark says wait (consulting, page loading, etc.)
            # ══════════════════════════════════════════════════════════
            elif dtype == "wait":
                seconds = max(directive.get("seconds", 3.0), 2.0)  # Floor at 2s
                reason = directive.get("reason", "")
                if prev_directive_type != "wait":
                    logger.info(f"Waiting ({reason})...")
                time.sleep(seconds)
                last_result = None  # No action taken

            # ══════════════════════════════════════════════════════════
            # NEED_SCREENSHOT: Spark needs screenshot for consultation
            # ══════════════════════════════════════════════════════════
            elif dtype == "need_screenshot":
                reason = directive.get("reason", "")
                logger.info(f"Capturing screenshot ({reason})")
                macapptree = capture_macapptree(app_name)
                pending_screenshot = macapptree.get("screenshot_b64", "")
                # KEEP last_result — Spark needs failure context with the screenshot.
                # Clearing it caused a ping-pong loop: failure -> need_screenshot ->
                # screenshot sent with last_result=None -> Spark re-matches -> execute_tree -> failure
                # Send immediately — no sleep
                continue

            # ══════════════════════════════════════════════════════════
            # CONSULTING: Consultation started, poll on next call
            # ══════════════════════════════════════════════════════════
            elif dtype == "consulting":
                new_consultation_id = directive.get("consultation_id")
                poll_interval = max(directive.get("poll_interval", 3.0), 2.0)  # Floor at 2s
                if new_consultation_id != active_consultation_id:
                    # Log once when consultation starts, not every poll
                    logger.info(f"Consulting Spark Claude ({new_consultation_id})...")
                    active_consultation_id = new_consultation_id
                time.sleep(poll_interval)
                last_result = None

            # ══════════════════════════════════════════════════════════
            # USER_INPUT_NEEDED: Show in chat panel, collect user text
            # ══════════════════════════════════════════════════════════
            elif dtype == "user_input_needed":
                screen_type = directive.get("screen_type", "UNKNOWN")
                tree_hash_for_dialog = directive.get("tree_hash", tree_hash)
                logger.info(f"User input needed for {screen_type}")

                user_text = ""

                # Prefer chat-based input (new)
                if user_input_callback:
                    try:
                        user_text = user_input_callback(directive)
                    except Exception as e:
                        logger.error(f"Chat user input error: {e}")
                # Fall back to legacy modal dialog
                elif user_escalation_callback:
                    try:
                        user_text = user_escalation_callback(
                            screen_type, tree_hash_for_dialog
                        )
                    except Exception as e:
                        logger.error(f"User escalation callback error: {e}")

                if user_text:
                    last_result = {
                        "directive_id": directive_id,
                        "success": False,
                        "user_response": user_text,
                        "screen": screen_type,
                    }
                else:
                    # No callback or user dismissed — keep polling.
                    # Spark may resolve the situation, or user will intervene manually.
                    logger.warning("No user input collected — waiting and re-polling")
                    last_result = None
                    time.sleep(5.0)

            # ══════════════════════════════════════════════════════════
            # STOP: Spark says we're done
            # ══════════════════════════════════════════════════════════
            elif dtype == "stop":
                reason = directive.get("reason", "server_stop")
                logger.info(f"Stop directive: {reason}")
                return {
                    "success": directive.get("success", False),
                    "reason": reason,
                    "screens_completed": screens_completed,
                    "message": directive.get("message", ""),
                    "detected": directive.get("detected_text", ""),
                }

            # ══════════════════════════════════════════════════════════
            # UNKNOWN: Unrecognized directive — wait and re-poll
            # ══════════════════════════════════════════════════════════
            else:
                logger.warning(f"Unknown directive type: {dtype} — waiting and re-polling")
                last_result = None
                time.sleep(5.0)

        except StaleElementError as e:
            logger.warning(f"Stale element: {e} — recapturing")
            time.sleep(1.0)
            continue

        except Exception as e:
            consecutive_errors += 1
            logger.error(f"Pipeline error #{consecutive_errors}: {e}", exc_info=True)
            if consecutive_errors >= 2:
                logger.error(f"PIPELINE FAILED: {consecutive_errors} consecutive errors. Stopping.")
                clear_checkpoint(platform, course_id, app_name)
                return {
                    "success": False,
                    "reason": "consecutive_errors",
                    "screens_completed": screens_completed,
                    "message": f"Pipeline stopped after {consecutive_errors} consecutive errors: {e}",
                }
            last_result = None
            time.sleep(5.0)
            continue

    # User stopped
    clear_checkpoint(platform, course_id, app_name)
    logger.info(f"=== Stopped by user. Screens: {screens_completed} ===")
    return {
        "success": True,
        "reason": "stopped_by_user",
        "screens_completed": screens_completed,
    }


if __name__ == "__main__":
    result = run_one_screen("khan_academy", "Google Chrome", platform_type="browser")
    print(result)
