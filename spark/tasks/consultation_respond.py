"""
Consultation response handling.

V11: Stores screen signatures (set-difference) after response.
Every consultation response teaches the signature store for future recognition.

Post-V8 fix (2026-02-20):
  - Updates metadata.json status to "complete" after writing response.json
    (fixes 1-at-a-time deadlock where new consultations were blocked forever)
"""

import json
import logging
from datetime import datetime
from pathlib import Path

from .atomic_write import atomic_write_json
from .consultation_state import get_consultation_state

logger = logging.getLogger(__name__)

CONSULT_DIR = Path("/tmp/taey-ed-consult")


def _update_screen_bt(
    consultation_id: str,
    tree: dict,
    consult_path: Path,
):
    """
    Update an existing screen signature with BT from consultation.

    Does NOT create new entries or change screen_type — Gemini classification
    owns the screen_type. Consultation only provides behavior trees.

    Non-fatal — failure here doesn't block the consultation response.
    """
    if not tree:
        return

    try:
        tree_file = consult_path / "tree.json"
        if not tree_file.exists():
            logger.warning(f"No tree.json for {consultation_id}, skipping BT update")
            return

        with open(tree_file) as f:
            ax_tree = json.load(f)

        meta_file = consult_path / "metadata.json"
        platform = "unknown"
        if meta_file.exists():
            with open(meta_file) as f:
                meta = json.load(f)
                platform = meta.get("platform", "unknown")

        from .screen_signatures import extract_signature, _sig_hash, _load_platform, _save_platform

        sig = extract_signature(ax_tree)
        sig_hash = _sig_hash(sig)
        data = _load_platform(platform)

        if sig_hash not in data["screens"]:
            logger.info(f"No existing signature {sig_hash[:12]} — skipping BT update")
            return

        existing = data["screens"][sig_hash]
        if not existing.get("behavior_tree"):
            existing["behavior_tree"] = tree
            existing["source"] = "consultation"
            _save_platform(platform, data)
            logger.info(
                f"Updated BT for {existing['screen_type']} ({sig_hash[:12]}) "
                f"from consultation {consultation_id}"
            )
        else:
            logger.info(f"Signature {sig_hash[:12]} already has BT — skipping")

    except Exception as e:
        logger.warning(f"Failed to update screen BT (non-fatal): {e}")


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

    # Update existing signature with BT (does not create new entries or change screen_type).
    _update_screen_bt(consultation_id, tree, consult_path)

    logger.info(f"Consultation responded: {consultation_id} → {screen_type}")

    return response
