"""
Consultation response handling.

Post-V8 fix (2026-02-20): updates metadata.json status to "complete" after
writing response.json (fixes 1-at-a-time deadlock where new consultations
were blocked forever).
"""

import json
import logging
from datetime import datetime
from pathlib import Path

from .atomic_write import atomic_write_json
from .consultation_state import get_consultation_state

logger = logging.getLogger(__name__)

CONSULT_DIR = Path("/tmp/taey-ed-consult")


def respond_to_consultation(
    consultation_id: str,
    screen_type: str,
    action: dict = None,
    requires_validation: bool = True,
    yaml_created: bool = False,
    extract: dict = None,
    tree: dict = None,
    expected_next: list = None,
) -> dict:
    """
    Create consultation response (called by Spark Claude).

    Args:
        consultation_id: The consultation ID
        screen_type: Name of the screen type
        action: Legacy action dict (prefer tree:)
        requires_validation: Whether Mac should send validation request after
        yaml_created: Whether YAML config was created/updated
        extract: Phase 5 extraction config (text criteria, image bbox, etc.)
        tree: V9 behavior tree for Mac to execute
        expected_next: List of screen_types that should follow this BT

    Returns:
        Response dict written to response.json
    """
    consult_path = CONSULT_DIR / consultation_id

    if not consult_path.exists():
        return {"error": f"Consultation {consultation_id} not found"}

    response = {
        "consultation_id": consultation_id,
        "screen_type": screen_type,
        "yaml_created": yaml_created,
        "requires_validation": requires_validation,
        "responded_at": datetime.now().isoformat()
    }

    # V9: Include behavior tree if provided
    if tree:
        response["tree"] = tree

    # Legacy: include action if provided
    if action:
        response["action"] = action

    # Include Phase 5 extraction config if provided
    if extract:
        response["extract"] = extract

    # Include expected_next for validation chain
    if expected_next:
        response["expected_next"] = expected_next

    # Write response file (atomic to prevent partial reads by Mac polling)
    atomic_write_json(consult_path / "response.json", response)

    # Update metadata status to "complete" so 1-at-a-time check doesn't deadlock
    meta_file = consult_path / "metadata.json"
    if meta_file.exists():
        try:
            meta = json.loads(meta_file.read_text())
            meta["status"] = "complete"
            meta["responded_at"] = response["responded_at"]
            atomic_write_json(meta_file, meta)
        except Exception as e:
            logger.warning(f"Failed to update metadata status (non-fatal): {e}")

    # Update state
    state = get_consultation_state(consultation_id)
    if state:
        state.spark_attempts += 1
        state.add_attempt("spark_claude", {"screen_type": screen_type, "action": action})

    logger.info(f"Consultation responded: {consultation_id} → {screen_type}")

    return response
