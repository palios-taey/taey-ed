# STATUS: FROZEN - V8 re-export module. Verified 2026-02-19. Do not modify.
"""
Handle consultation requests for unknown screens.

Re-export module for backwards compatibility.
The actual implementation is split into:
- consultation_state.py - State tracking
- consultation_request.py - Request handling (uses prompt_codex for V8)
- consultation_respond.py - Response handling
- consultation_escalate.py - Escalation handling
"""

from .consultation_state import (
    ConsultationState,
    get_consultation_state,
    set_consultation_state,
    compute_tree_hash,
)
from .consultation_request import (
    request_consultation,
    check_consultation,
    get_pending_consultations,
    CONSULT_DIR,
)
from .consultation_respond import respond_to_consultation
from .consultation_escalate import escalate_consultation

__all__ = [
    # State
    "ConsultationState",
    "get_consultation_state",
    "set_consultation_state",
    "compute_tree_hash",
    # Request
    "request_consultation",
    "check_consultation",
    "get_pending_consultations",
    "CONSULT_DIR",
    # Respond
    "respond_to_consultation",
    # Escalate
    "escalate_consultation",
]
