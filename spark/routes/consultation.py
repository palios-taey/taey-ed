# STATUS: FROZEN - V8 consultation routes. Verified 2026-02-19. Do not modify.
"""Consultation CRUD endpoints."""

import json
import logging
from pathlib import Path

from fastapi import APIRouter, HTTPException

from spark.models import ConsultRequest, ConsultResponseRequest, EscalateRequest
from spark.tasks.handle_consultation import (
    request_consultation,
    check_consultation,
    respond_to_consultation,
    escalate_consultation,
    get_pending_consultations,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1")


@router.post("/consult")
def submit_consultation(request: ConsultRequest):
    """Submit consultation for unknown screen."""
    return request_consultation(
        platform=request.platform,
        tree=request.tree,
        screenshot_b64=request.screenshot_b64,
        context=request.context,
        bt_debug_log=request.bt_debug_log,
    )


@router.get("/consult/{consultation_id}")
def poll_consultation(consultation_id: str):
    """Poll for consultation response."""
    result = check_consultation(consultation_id)
    if result.get("status") == "not_found":
        raise HTTPException(status_code=404, detail=result.get("error"))
    return result


@router.post("/consult/{consultation_id}/respond")
def respond_consultation(consultation_id: str, request: ConsultResponseRequest):
    """
    Respond to consultation (Spark Claude calls this).
    Rejects trees containing fallback nodes.
    """
    # Reject fallback nodes
    if request.tree:
        from spark.tasks.validate_config import validate_tree_no_fallbacks
        fallback_errors = validate_tree_no_fallbacks(request.tree)
        if fallback_errors:
            raise HTTPException(
                status_code=400,
                detail=f"Tree rejected: {'; '.join(str(e) for e in fallback_errors)}. "
                       f"Use strict sequences — every action must succeed or escalate."
            )

    # Content list check — warn but don't block
    if request.tree:
        try:
            from spark.tasks.validate_config import validate_bt_for_screen
            tree_file = Path(f"/tmp/taey-ed-consult/{consultation_id}/tree.json")
            if tree_file.exists():
                ax_tree = json.loads(tree_file.read_text())
                bt_errors = validate_bt_for_screen(request.tree, ax_tree)
                if bt_errors:
                    logger.warning(
                        f"BT content list warning (allowing): "
                        f"{'; '.join(str(e) for e in bt_errors)}"
                    )
        except Exception as e:
            logger.warning(f"BT validation check failed (non-fatal): {e}")

    result = respond_to_consultation(
        consultation_id=consultation_id,
        screen_type=request.screen_type,
        action=request.action,
        requires_validation=request.requires_validation,
        yaml_created=True,
        extract=request.extract,
        tree=request.tree,
        expected_next=request.expected_next,
    )
    if "error" in result:
        raise HTTPException(status_code=404, detail=result["error"])

    # Add course_id to response file if provided
    if request.course_id:
        response_file = Path(f"/tmp/taey-ed-consult/{consultation_id}/response.json")
        if response_file.exists():
            data = json.loads(response_file.read_text())
            data["course_id"] = request.course_id
            response_file.write_text(json.dumps(data, indent=2))
        result["course_id"] = request.course_id

    return result


@router.post("/consult/{consultation_id}/escalate")
def escalate(consultation_id: str, request: EscalateRequest):
    """Escalate consultation to next level."""
    result = escalate_consultation(consultation_id, request.reason)
    if "error" in result:
        raise HTTPException(status_code=404, detail=result["error"])
    return result


@router.get("/consultations/pending")
def list_pending_consultations():
    """List all pending consultations."""
    return {"pending": get_pending_consultations()}
