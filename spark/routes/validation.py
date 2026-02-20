# STATUS: FROZEN - V8 validation routes. Verified 2026-02-19. Do not modify.
"""Validation flow endpoints — post-action tree validation and learning loop."""

import json
import logging
from pathlib import Path

from fastapi import APIRouter, HTTPException

from spark.models import ValidateRequest

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1")


@router.post("/validate")
def submit_validation(request: ValidateRequest):
    """
    Submit validation request after action execution.

    On success, stores the skeleton embedding in Weaviate (learning loop).
    """
    from spark.tasks.handle_consultation import check_consultation
    from spark.tasks.screen_collapse import collapse as collapse_screen

    # Import validate_action lazily to avoid circular imports
    from spark.tasks.validate_action import validate_action

    result = validate_action(
        consultation_id=request.consultation_id,
        action_executed=request.action_executed,
        before_tree_hash=request.before_tree_hash,
        after_tree=request.after_tree,
        after_screenshot_b64=request.after_screenshot_b64,
    )

    # Learning loop: on successful validation, collapse into Weaviate
    if result.get("validated") and result.get("screen_transitioned"):
        try:
            consult_path = Path("/tmp/taey-ed-consult") / request.consultation_id
            tree_file = consult_path / "tree.json"
            response_file = consult_path / "response.json"
            metadata_file = consult_path / "metadata.json"

            if tree_file.exists() and response_file.exists():
                before_tree = json.loads(tree_file.read_text())
                response_data = json.loads(response_file.read_text())
                metadata = json.loads(metadata_file.read_text()) if metadata_file.exists() else {}
                platform = metadata.get("platform", "unknown")
                behavior_tree = response_data.get("tree", {})

                if behavior_tree:
                    from spark.tasks.skeleton import extract_skeleton, skeleton_hash as skel_hash
                    from spark.tasks.screen_memory import embed_text

                    skel = extract_skeleton(before_tree)
                    shash = skel_hash(skel)
                    vec = embed_text(skel)

                    collapse_screen(
                        tree_before=before_tree,
                        tree_after=request.after_tree,
                        embedding=vec,
                        behavior_tree=behavior_tree,
                        platform=platform,
                        skeleton_text=skel,
                        skeleton_hash_val=shash,
                    )
        except Exception as e:
            logger.warning(f"Spark-side collapse failed (non-fatal): {e}")

    return result


@router.get("/validate/{consultation_id}/{validation_id}")
def poll_validation(consultation_id: str, validation_id: str):
    """Poll for validation response."""
    from spark.tasks.validate_action import check_validation

    result = check_validation(consultation_id, validation_id)
    if result.get("status") == "not_found":
        raise HTTPException(status_code=404, detail=result.get("error"))
    return result
