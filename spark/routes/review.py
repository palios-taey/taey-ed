# STATUS: FROZEN - V8 action review routes. Verified 2026-02-19. Do not modify.
"""Action review endpoints — post-action validation failure handling."""

from fastapi import APIRouter, HTTPException

from spark.models import ActionReviewRequest, ActionReviewResponseRequest
from spark.tasks.action_review import save_action_review, check_review, respond_to_review

router = APIRouter(prefix="/api/v1")


@router.post("/action_review")
def submit_action_review(request: ActionReviewRequest):
    """Submit action review when post-action validation fails."""
    return save_action_review(
        platform=request.platform,
        before_screen=request.before_screen,
        action_taken=request.action_taken,
        after_screen=request.after_screen,
        expected_next=request.expected_next,
        after_tree=request.after_tree,
        after_screenshot_b64=request.after_screenshot_b64,
        failure_reason=request.failure_reason,
        escalation_level=request.escalation_level,
        user_message=request.user_message,
        question_text=request.question_text,
        answer_generated=request.answer_generated,
        options_presented=request.options_presented,
        click_target=request.click_target,
        bt_debug_log=request.bt_debug_log,
    )


@router.get("/action_review/{platform}/{review_id}")
def poll_action_review(platform: str, review_id: str):
    """Poll for action review response."""
    result = check_review(review_id, platform)
    if result.get("status") == "not_found":
        raise HTTPException(status_code=404, detail=result.get("error"))
    return result


@router.post("/action_review/{platform}/{review_id}/respond")
def respond_action_review(
    platform: str,
    review_id: str,
    request: ActionReviewResponseRequest,
):
    """Respond to action review."""
    result = respond_to_review(
        review_id=review_id,
        platform=platform,
        resolution=request.resolution,
        retry=request.retry,
        corrected_answer=request.corrected_answer,
        yaml_updates=request.yaml_updates,
        message=request.message,
    )
    if "error" in result:
        raise HTTPException(status_code=404, detail=result["error"])
    return result
