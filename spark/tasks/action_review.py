# STATUS: FROZEN. Verified 2026-02-19. Do not modify.
"""
Post-action validation review.
Phase 7: Mac sends escalation when action didn't produce expected screen.

Saves review files to /tmp/taey-ed-reviews/{platform}/{review_id}/
Notifies Spark Claude via tmux for autonomous review.

Mac POLLS for response and retries if retry=True.

V10: Embeds after_tree into Weaviate ScreenEmbedding on review response.
Every review response teaches the vector store for future recognition.
"""

import json
import os
import time
import base64
import logging

from .atomic_write import atomic_write_json
from .notify_tmux import notify_spark_claude

logger = logging.getLogger(__name__)

REVIEWS_DIR = "/tmp/taey-ed-reviews"


def save_action_review(
    platform: str,
    before_screen: str,
    action_taken: dict,
    after_screen: str,
    expected_next: list,
    after_tree: dict,
    after_screenshot_b64: str,
    failure_reason: str,
    escalation_level: str = "spark_claude",
    user_message: str = "",
    question_text: str = "",
    answer_generated: str = "",
    options_presented: list = None,
    click_target: str = "",
    bt_debug_log: str = "",
) -> dict:
    """
    Save action review for Spark Claude to investigate.

    Returns: {"review_id": str, "status": "pending"}
    """
    # no_match means forward progress to a NEW unmatched screen — not a repeated
    # failure of the same screen. Reset escalation to spark_claude so it gets
    # treated as a fresh consultation instead of a Perplexity escalation.
    if failure_reason == "no_match" and escalation_level != "spark_claude":
        logger.info(f"no_match failure: resetting escalation from {escalation_level} to spark_claude (new screen, not repeated failure)")
        escalation_level = "spark_claude"

    review_id = f"review_{int(time.time())}_{os.urandom(4).hex()}"
    review_dir = os.path.join(REVIEWS_DIR, platform, review_id)
    os.makedirs(review_dir, exist_ok=True)

    # Save metadata
    metadata = {
        "review_id": review_id,
        "platform": platform,
        "before_screen": before_screen,
        "action_taken": action_taken,
        "after_screen": after_screen,
        "expected_next": expected_next,
        "failure_reason": failure_reason,
        "escalation_level": escalation_level,
        "user_message": user_message,
        "question_text": question_text,
        "answer_generated": answer_generated,
        "options_presented": options_presented or [],
        "click_target": click_target,
        "status": "pending",
        "created_at": time.time(),
    }
    atomic_write_json(os.path.join(review_dir, "metadata.json"), metadata)

    # Save tree
    atomic_write_json(os.path.join(review_dir, "after_tree.json"), after_tree, indent=0)

    # Save screenshot
    if after_screenshot_b64:
        with open(os.path.join(review_dir, "screenshot.png"), "wb") as f:
            f.write(base64.b64decode(after_screenshot_b64))

    # Save BT debug log (behavior tree execution trace from Mac)
    if bt_debug_log:
        with open(os.path.join(review_dir, "bt_debug.log"), "w") as f:
            f.write(bt_debug_log)

    # Build tmux notification — separate path for user-reported issues vs escalation chain
    if failure_reason == "user_feedback":
        diag = _build_user_report_notification(
            review_id=review_id,
            platform=platform,
            user_message=user_message,
            review_dir=review_dir,
        )
    else:
        diag = _build_escalation_notification(
            review_id=review_id,
            platform=platform,
            escalation_level=escalation_level,
            user_message=user_message,
            failure_reason=failure_reason,
            before_screen=before_screen,
            after_screen=after_screen,
            question_text=question_text,
            answer_generated=answer_generated,
            options_presented=options_presented,
            click_target=click_target,
            review_dir=review_dir,
        )
    notify_spark_claude(diag)

    logger.info(f"Action review saved: {review_id} ({failure_reason})")

    # Rolling cleanup: keep only 2 most recent completed reviews per platform
    _cleanup_old_reviews(platform, keep=2)

    return {"review_id": review_id, "status": "pending"}


def check_review(review_id: str, platform: str) -> dict:
    """Poll for review response. Mac calls this until status != pending."""
    review_dir = os.path.join(REVIEWS_DIR, platform, review_id)
    response_path = os.path.join(review_dir, "response.json")
    metadata_path = os.path.join(review_dir, "metadata.json")

    if not os.path.exists(metadata_path):
        return {"status": "not_found", "error": f"Review {review_id} not found"}

    if os.path.exists(response_path):
        with open(response_path) as f:
            response = json.load(f)
        return {"status": "complete", **response}

    return {"status": "pending", "review_id": review_id}


def respond_to_review(
    review_id: str,
    platform: str,
    resolution: str,
    retry: bool = False,
    corrected_answer: str = "",
    yaml_updates: str = "",
    message: str = "",
) -> dict:
    """
    Spark Claude responds to a review.

    resolution: "yaml_updated" | "answer_corrected" | "escalated" | "acknowledged"
    retry: True = Mac should retry the action (with corrected_answer if provided)
    """
    review_dir = os.path.join(REVIEWS_DIR, platform, review_id)
    metadata_path = os.path.join(review_dir, "metadata.json")

    if not os.path.exists(metadata_path):
        return {"error": f"Review {review_id} not found"}

    response = {
        "resolution": resolution,
        "retry": retry,
        "corrected_answer": corrected_answer,
        "yaml_updates": yaml_updates,
        "message": message,
        "responded_at": time.time(),
    }

    atomic_write_json(os.path.join(review_dir, "response.json"), response)

    # Update metadata status (atomic read-modify-write)
    with open(metadata_path) as f:
        metadata = json.load(f)
    metadata["status"] = "complete"
    atomic_write_json(metadata_path, metadata)

    logger.info(f"Review {review_id} resolved: {resolution} (retry={retry})")

    # NOTE: _embed_review_to_weaviate was removed — it stored screens with
    # empty behavior_tree={} which poisoned vector search (matched but no BT).
    # Learning now happens only through consultation_respond (provisional)
    # and validation confirmation in /next_action (mark_validated).

    return {"status": "complete", "review_id": review_id, "resolution": resolution, "retry": retry}


# =============================================================================
# NOTIFICATION BUILDERS
# =============================================================================

def _build_user_report_notification(
    review_id: str,
    platform: str,
    user_message: str,
    review_dir: str,
) -> str:
    """
    Build tmux notification for a user-reported issue (Report Issue button).

    This is NOT an escalation — the user proactively reported a problem.
    The prompt is simpler and focused on the user's description + screenshot/tree.
    """
    return (
        f"USER REPORTED ISSUE for platform {platform}\n\n"
        f"Use the Task tool NOW to launch an agent (subagent_type=general-purpose) with this prompt:\n\n"
        f"\"A user reported an issue via the Report Issue button for platform {platform}.\n"
        f"USER SAYS: {user_message}\n\n"
        f"Look at the screenshot and accessibility tree to understand what the user is seeing.\n"
        f"Then fix the problem — either update an existing screen mapping in the YAML config,\n"
        f"or create a new screen mapping if this screen is not yet recognized.\n\n"
        f"READ /home/user/taey-ed/CLAUDE.md FIRST.\n"
        f"READ /home/user/taey-ed/spark/platforms/{platform}/knowledge.json for platform knowledge.\n"
        f"ALL screens use tree: sections (V9 format). See Section 4 for handler names.\n\n"
        f"SCREENSHOT: {review_dir}/screenshot.png (READ THIS WITH Read TOOL — you CAN view images. LOOK at the visual layout to understand what the screen shows.)\n"
        f"Review files (screenshot + tree + bt_debug.log): {review_dir}/\n"
        f"BT DEBUG LOG: {review_dir}/bt_debug.log (READ THIS FIRST — shows exact handler execution trace)\n"
        f"Platform config: /home/user/taey-ed/spark/platforms/{platform}/config.yaml\n"
        f"Respond via API: POST http://127.0.0.1:5003/api/v1/action_review/{platform}/{review_id}/respond\n"
        f"Response format: {{\\\"resolution\\\": \\\"yaml_updated|acknowledged\\\", \\\"retry\\\": true, \\\"message\\\": \\\"...\\\"}}\""
    )


def _build_escalation_notification(
    review_id: str,
    platform: str,
    escalation_level: str,
    user_message: str,
    failure_reason: str,
    before_screen: str,
    after_screen: str,
    question_text: str,
    answer_generated: str,
    options_presented: list,
    click_target: str,
    review_dir: str,
) -> str:
    """
    Build tmux notification for automated escalation chain (tier 1/2/3).

    This is the original escalation flow triggered by validation failures,
    NOT user-initiated reports.
    """
    escalation_header = f"ESCALATION LEVEL: {escalation_level.upper()}"
    if escalation_level == "perplexity":
        escalation_header += " — USE PERPLEXITY DEEP RESEARCH before responding"
    elif escalation_level == "user":
        escalation_header += f" — USER GUIDANCE: {user_message}"

    diag = (
        f"{escalation_header}\n\n"
        f"Use the Task tool NOW to launch an agent (subagent_type=general-purpose) with this prompt:\n\n"
        f"\"Handle Taey-Ed action review {review_id} for platform {platform}.\n"
        f"ESCALATION: {escalation_level}"
    )
    if escalation_level == "perplexity":
        diag += (
            f"\nThis is a PERPLEXITY ESCALATION. Previous Spark Claude fix failed.\n"
            f"You MUST use Perplexity Deep Research for additional context before responding.\n"
            f"Follow the escalation prompt template at /home/user/taey-ed/spark/ESCALATION_PROMPT.md\n"
            f"Attach: MASTER_PLAN.md, CLAUDE.md, platform config, screenshots, issue description.\n"
        )
    elif escalation_level == "user":
        diag += (
            f"\nThis is a USER ESCALATION. Both Spark and Perplexity fixes failed.\n"
            f"The user provided this guidance: {user_message}\n"
            f"Incorporate the user's guidance to fix the YAML config and respond with retry=true.\n"
        )
    diag += (
        f"\nREAD /home/user/taey-ed/CLAUDE.md FIRST.\n"
        f"READ /home/user/taey-ed/spark/platforms/{platform}/knowledge.json for platform knowledge.\n"
        f"ALL screens use tree: sections format. See handler docs for handler names and examples.\n"
        f"Failure: {failure_reason} | Screen: {before_screen} -> {after_screen}\n"
        f"Question: {question_text[:120]}\n"
        f"Answer: {answer_generated} | Options: {options_presented} | Clicked: {click_target}\n"
        f"SCREENSHOT: {review_dir}/screenshot.png (READ THIS WITH Read TOOL — you CAN view images. LOOK at the visual layout to understand what the screen shows.)\n"
        f"Review files: {review_dir}/\n"
        f"BT DEBUG LOG: {review_dir}/bt_debug.log (READ THIS FIRST — shows exact handler execution trace)\n"
        f"Platform config: /home/user/taey-ed/spark/platforms/{platform}/config.yaml\n"
        f"Respond via API: POST http://127.0.0.1:5003/api/v1/action_review/{platform}/{review_id}/respond\n"
        f"Response format: {{\\\"resolution\\\": \\\"yaml_updated|acknowledged\\\", \\\"retry\\\": true|false, \\\"message\\\": \\\"...\\\"}}\""
    )
    return diag


def _cleanup_old_reviews(platform: str, keep: int = 2):
    """Remove old completed reviews for a platform, keeping the most recent `keep`."""
    import shutil

    platform_dir = os.path.join(REVIEWS_DIR, platform)
    if not os.path.isdir(platform_dir):
        return

    completed = []
    for name in os.listdir(platform_dir):
        review_path = os.path.join(platform_dir, name)
        if not os.path.isdir(review_path):
            continue
        response_file = os.path.join(review_path, "response.json")
        if not os.path.exists(response_file):
            continue  # Keep pending reviews
        meta_file = os.path.join(review_path, "metadata.json")
        ts = 0.0
        if os.path.exists(meta_file):
            try:
                with open(meta_file) as f:
                    ts = json.load(f).get("created_at", 0.0)
            except Exception:
                pass
        completed.append((ts, review_path))

    completed.sort(key=lambda x: x[0], reverse=True)
    for _, path in completed[keep:]:
        try:
            shutil.rmtree(path)
            logger.info(f"Cleaned up old review: {os.path.basename(path)}")
        except Exception as e:
            logger.warning(f"Failed to clean up {path}: {e}")
