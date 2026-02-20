# STATUS: FROZEN - Proven in v7. Verified 2026-02-19. Do not modify.
"""
Consultation escalation handling.

LOCKED FILE - Do not modify without Jesse's approval.
This handles escalating consultations through the escalation path.
"""

import json
import logging
from pathlib import Path

from .atomic_write import atomic_write_json
from .consultation_state import get_consultation_state
from .notify_tmux import notify_spark_claude

logger = logging.getLogger(__name__)

CONSULT_DIR = Path("/tmp/taey-ed-consult")


def escalate_consultation(consultation_id: str, reason: str) -> dict:
    """
    Escalate consultation to next level.

    Escalation path: Spark Claude → Perplexity → User

    Args:
        consultation_id: The consultation ID
        reason: Why escalation is needed

    Returns:
        Updated status
    """
    consult_path = CONSULT_DIR / consultation_id

    if not consult_path.exists():
        return {"error": f"Consultation {consultation_id} not found"}

    state = get_consultation_state(consultation_id)
    if not state:
        return {"error": "Consultation state not found"}

    next_level = state.next_escalation()

    # Update metadata
    metadata_file = consult_path / "metadata.json"
    if metadata_file.exists():
        metadata = json.loads(metadata_file.read_text())
        metadata["escalation_level"] = next_level
        metadata["spark_attempts"] = state.spark_attempts
        metadata["escalation_reason"] = reason
        atomic_write_json(metadata_file, metadata)

    if next_level == "perplexity":
        state.perplexity_attempted = True
        notify_spark_claude(f"[ESCALATE] {consultation_id}: Escalating to Perplexity - {reason[:40]}")
    elif next_level == "user":
        state.user_escalated = True
        notify_spark_claude(f"[USER REQUIRED] {consultation_id}: {reason[:40]}")

    logger.info(f"Consultation escalated: {consultation_id} → {next_level}")

    return {
        "consultation_id": consultation_id,
        "escalation_level": next_level,
        "reason": reason
    }
