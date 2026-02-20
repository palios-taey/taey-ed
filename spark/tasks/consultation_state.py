# STATUS: FROZEN - Proven in v7. Verified 2026-02-19. Do not modify.
"""
Consultation state tracking.

LOCKED FILE - Do not modify without Jesse's approval.
This defines the core state structure for consultations.
"""

import hashlib
from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, List


@dataclass
class ConsultationState:
    """Track consultation escalation state."""
    consultation_id: str
    screen_hash: str  # SHA256 of relevant tree elements
    platform: str

    # Escalation tracking (no arbitrary limits)
    spark_attempts: int = 0
    perplexity_attempted: bool = False
    user_escalated: bool = False

    # Validation
    validated: bool = False
    validation_attempts: int = 0

    # History
    attempts: List[dict] = field(default_factory=list)

    def next_escalation(self) -> str:
        """Determine next escalation level."""
        if self.spark_attempts < 2:
            return "spark_claude"
        elif not self.perplexity_attempted:
            return "perplexity"
        else:
            return "user"

    def add_attempt(self, level: str, result: dict):
        """Record attempt with full context."""
        self.attempts.append({
            "timestamp": datetime.now().isoformat(),
            "level": level,
            "result": result
        })


# In-memory state tracking (per-session)
_consultations: Dict[str, ConsultationState] = {}


def get_consultation_state(consultation_id: str) -> ConsultationState:
    """Get consultation state by ID."""
    return _consultations.get(consultation_id)


def set_consultation_state(consultation_id: str, state: ConsultationState):
    """Store consultation state."""
    _consultations[consultation_id] = state


def compute_tree_hash(tree: dict) -> str:
    """Compute hash of relevant tree elements for deduplication."""
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
