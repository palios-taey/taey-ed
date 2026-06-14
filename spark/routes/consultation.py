# STATUS: FROZEN. Consultation routes. Verified 2026-02-19. Do not modify.
# 2026-05-08 unfreeze: added /abandon_consultation/{id} endpoint to support
# Mac lifecycle fix (CCM commit 084de95). Re-locked after.
"""Consultation CRUD endpoints."""

import json
import logging
from datetime import datetime
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
from spark.tasks.atomic_write import atomic_write_json

logger = logging.getLogger(__name__)

CONSULT_DIR = Path("/tmp/taey-ed-consult")

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
    Supervisor responses are definitions/classifications only.
    """
    result = respond_to_consultation(
        consultation_id=consultation_id,
        screen_type=request.screen_type,
        requires_validation=request.requires_validation,
        yaml_created=True,
        extract=request.extract,
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


@router.post("/abandon_consultation/{consultation_id}")
def abandon_consultation(consultation_id: str):
    """Mark a consultation as abandoned.

    Called by Mac on graceful shutdown (red X / Cmd+Q / pipeline stop) so that
    the ONE-AT-A-TIME consultation gate releases instead of staying blocked on
    a never-resolving consult. Idempotent: returns 200 if already completed,
    abandoned, or missing.

    Per CCM lifecycle fix (Mac commit 084de95).
    """
    consult_path = CONSULT_DIR / consultation_id
    meta_file = consult_path / "metadata.json"

    if not meta_file.exists():
        # Idempotent: nothing to abandon.
        return {"ok": True, "status": "not_found", "consultation_id": consultation_id}

    try:
        meta = json.loads(meta_file.read_text())
    except Exception as e:
        logger.warning(f"abandon_consultation: failed to read metadata for {consultation_id}: {e}")
        raise HTTPException(status_code=500, detail=f"metadata read failed: {e}")

    current_status = meta.get("status", "")
    if current_status in ("complete", "completed", "abandoned"):
        # Idempotent: already terminal.
        return {"ok": True, "status": current_status, "consultation_id": consultation_id}

    meta["status"] = "abandoned"
    meta["abandoned_at"] = datetime.now().isoformat()
    atomic_write_json(meta_file, meta)

    # user-Stop is one of the two legitimate escalation resets (Jesse 2026-06-14):
    # abandon = the user stopped, so clear this screen's escalation counter +
    # terminal so a restart begins fresh. (The other reset is genuine advance.)
    try:
        from spark.tasks import escalation_state
        _h = meta.get("screen_hash") or ""
        if _h:
            escalation_state.clear(meta.get("platform", "khan_academy"), _h, "user_stop_abandon")
    except Exception:
        logger.exception("abandon: escalation_state clear failed (non-fatal)")

    logger.info(f"Consultation {consultation_id} abandoned by Mac")
    return {"ok": True, "status": "abandoned", "consultation_id": consultation_id}
