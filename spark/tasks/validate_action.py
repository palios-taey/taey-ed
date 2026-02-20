# STATUS: FROZEN - Copied from v7. Verified 2026-02-19. Do not modify.
"""
Validate action results for consultation flow.
FREEZE once working.

Automated validation:
1. Compare tree hashes — did the action change anything?
2. Match after_tree against YAML — what screen are we on now?
3. If matched: validated=True (screen known, normal path takes over)
4. If not matched: validated=False (needs new consultation)
5. If unchanged: validated=False, reason=same_screen (action failed)
"""

import json
import logging
import os
from datetime import datetime
from pathlib import Path

from .atomic_write import atomic_write_json

logger = logging.getLogger(__name__)

CONSULT_DIR = Path("/tmp/taey-ed-consult")


def validate_action(
    consultation_id: str,
    action_executed: dict,
    before_tree_hash: str,
    after_tree: dict,
    after_screenshot_b64: str
) -> dict:
    """
    Automated validation of action result.

    Compares before/after tree hashes and matches after_tree against YAML.
    Returns immediately — no manual review needed.

    Args:
        consultation_id: Original consultation ID
        action_executed: What action was taken {"type": "click", "target": "..."}
        before_tree_hash: Hash of tree before action
        after_tree: Full accessibility tree after action
        after_screenshot_b64: Screenshot after action

    Returns:
        {"validated": True, "new_screen_type": str, "action": dict} on success
        {"validated": False, "reason": str} on failure
    """
    consult_path = CONSULT_DIR / consultation_id

    if not consult_path.exists():
        return {"error": f"Consultation {consultation_id} not found"}

    # Compute after-tree hash
    after_tree_hash = _compute_tree_hash(after_tree)
    tree_changed = before_tree_hash != after_tree_hash

    # Save validation data for debugging
    validation_id = f"validation_{int(datetime.now().timestamp())}_{os.urandom(4).hex()}"
    validation_path = consult_path / "validations" / validation_id
    validation_path.mkdir(parents=True, exist_ok=True)

    if after_screenshot_b64:
        import base64
        try:
            screenshot_bytes = base64.b64decode(after_screenshot_b64)
            (validation_path / "screenshot.png").write_bytes(screenshot_bytes)
        except Exception as e:
            logger.error(f"Failed to save validation screenshot: {e}")

    atomic_write_json(validation_path / "tree.json", after_tree)

    validation_request = {
        "consultation_id": consultation_id,
        "validation_id": validation_id,
        "action_executed": action_executed,
        "before_tree_hash": before_tree_hash,
        "after_tree_hash": after_tree_hash,
        "tree_changed": tree_changed,
        "timestamp": datetime.now().isoformat(),
    }
    atomic_write_json(validation_path / "request.json", validation_request)

    # === AUTOMATED VALIDATION ===

    if not tree_changed:
        result = {
            "validated": False,
            "reason": "same_screen",
            "message": "Tree unchanged — action had no effect",
            "validation_id": validation_id,
        }
        atomic_write_json(validation_path / "response.json", result)
        logger.info(f"Validation: {consultation_id}/{validation_id} -> same_screen (no change)")
        return result

    # Tree changed — match after_tree against YAML config
    metadata_file = consult_path / "metadata.json"
    try:
        metadata = json.loads(metadata_file.read_text())
        platform = metadata.get("platform")
    except Exception as e:
        logger.error(f"Failed to read consultation metadata: {e}")
        # Tree changed but can't determine platform — assume success
        result = {
            "validated": True,
            "reason": "tree_changed_no_metadata",
            "message": "Tree changed but could not read platform metadata — assuming success",
            "validation_id": validation_id,
        }
        atomic_write_json(validation_path / "response.json", result)
        return result

    from .load_yaml import load_yaml
    from .match_screen import match_screen

    config = load_yaml(platform)
    match_result = match_screen(after_tree, config)

    if match_result.get("matched"):
        result = {
            "validated": True,
            "screen_transitioned": True,
            "new_screen_type": match_result["screen"],
            "action": match_result.get("action"),
            "message": f"Transitioned to {match_result['screen']}",
            "validation_id": validation_id,
        }
    else:
        # Tree changed but no YAML match — needs new consultation
        result = {
            "validated": False,
            "reason": "no_match_after_transition",
            "message": "Tree changed but new screen not in YAML — needs consultation",
            "validation_id": validation_id,
        }

    atomic_write_json(validation_path / "response.json", result)
    logger.info(
        f"Validation: {consultation_id}/{validation_id} -> "
        f"validated={result.get('validated')} {result.get('message')}"
    )

    return result


def check_validation(consultation_id: str, validation_id: str) -> dict:
    """
    Check validation response status.

    Returns:
        Response dict if ready, or pending status
    """
    validation_path = CONSULT_DIR / consultation_id / "validations" / validation_id

    if not validation_path.exists():
        return {"status": "not_found", "error": f"Validation {validation_id} not found"}

    response_file = validation_path / "response.json"
    if response_file.exists():
        try:
            response = json.loads(response_file.read_text())
            return {"status": "complete", **response}
        except Exception as e:
            return {"status": "error", "error": str(e)}

    return {"status": "pending_review", "message": "Awaiting Spark Claude review"}


def _compute_tree_hash(tree: dict) -> str:
    """Compute hash of relevant tree elements."""
    import hashlib

    def extract_relevant(node: dict) -> list:
        result = []
        role = node.get("role", "")
        name = node.get("name", "")
        if role or name:
            result.append(f"{role}:{name}")
        for child in node.get("children", []):
            result.extend(extract_relevant(child))
        return result

    relevant = sorted(extract_relevant(tree))
    content = "|".join(relevant)
    return hashlib.sha256(content.encode()).hexdigest()[:16]
