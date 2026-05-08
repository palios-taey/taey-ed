# STATUS: FROZEN. Request models. Verified 2026-02-19. Do not modify.
"""
Pydantic request models for the Taey-Ed API.

Pydantic request/response shapes for the API.
"""

from pydantic import BaseModel
from typing import Optional, Dict, List, Union


# ── Screen Matching (Legacy) ──

class MatchRequest(BaseModel):
    platform: str
    tree: dict


# ── Consultation ──

class ConsultRequest(BaseModel):
    platform: str
    tree: dict
    screenshot_b64: str
    context: Optional[dict] = {}
    bt_debug_log: Optional[str] = ""


class ConsultResponseRequest(BaseModel):
    """For Spark Claude to respond to a consultation."""
    screen_type: str
    action: Optional[dict] = None
    tree: Optional[dict] = None
    requires_validation: bool = True
    extract: Optional[dict] = None
    course_id: Optional[str] = None
    expected_next: Optional[list] = None


class EscalateRequest(BaseModel):
    reason: str


# ── Validation ──

class ValidateRequest(BaseModel):
    consultation_id: str
    action_executed: dict
    before_tree_hash: str
    after_tree: dict
    after_screenshot_b64: str


# ── Compute (Phase 5-6) ──

class ExtractImageRequest(BaseModel):
    """Image content extraction via Gemini 2.5 Pro."""
    image_b64: str
    purpose: Optional[str] = None
    context: Optional[str] = None


class EmbedRequest(BaseModel):
    """Text embedding via Qwen3-Embedding-8B."""
    texts: Union[str, List[str]]


class GenerateRequest(BaseModel):
    """Answer generation via Gemini."""
    question: str
    question_type: str
    options: Optional[List[str]] = None
    context: Optional[List[str]] = None
    image_descriptions: Optional[List[str]] = None
    has_text_field: bool = False
    screen_config: Optional[Dict] = None
    items: Optional[List[Dict]] = None
    screenshot_b64: Optional[str] = None


# ── Action Review (Phase 7) ──

class ActionReviewRequest(BaseModel):
    """Post-action validation failure."""
    platform: str
    before_screen: str
    action_taken: dict
    after_screen: str
    expected_next: List[str]
    after_tree: dict
    after_screenshot_b64: str
    failure_reason: str
    escalation_level: Optional[str] = "spark_claude"
    user_message: Optional[str] = ""
    question_text: Optional[str] = ""
    answer_generated: Optional[str] = ""
    options_presented: Optional[list] = None
    click_target: Optional[str] = ""
    bt_debug_log: Optional[str] = ""


class ActionReviewResponseRequest(BaseModel):
    """Spark Claude responds to action review."""
    resolution: str
    retry: bool = False
    corrected_answer: Optional[str] = ""
    yaml_updates: Optional[str] = ""
    message: Optional[str] = ""


# ── Spinal Cord (Phase 8) ──

class RouteRequest(BaseModel):
    """Route a screen through embedding-based recognition."""
    platform: str
    tree: dict
    viewport_height: int = 900


class CollapseRequest(BaseModel):
    """Post-action collapse: store successful BT if screen changed."""
    platform: str
    tree_before: dict
    tree_after: dict
    embedding: List[float]
    behavior_tree: dict
    skeleton_text: str = ""
    skeleton_hash: str = ""


# ── Next Action (Directive Model) ──

class ClientState(BaseModel):
    screens_completed: int = 0
    last_tree_hash: Optional[str] = None
    course_id: str = "unknown"
    platform_type: str = "browser"
    active_consultation_id: Optional[str] = None


class LastResult(BaseModel):
    directive_id: Optional[str] = None
    success: Optional[bool] = None
    action: Optional[str] = None
    screen: Optional[str] = None
    tree_hash_before: Optional[str] = None
    tree_hash_after: Optional[str] = None
    continue_loop: bool = False
    user_response: Optional[str] = None
    after_tree: Optional[dict] = None
    directive_skeleton_hash: Optional[str] = None
    directive_expected_next: Optional[list] = None
    bt_debug_tail: Optional[str] = None  # Last N lines of /tmp/behavior_tree_debug.log
    failed_bt: Optional[dict] = None  # BT that failed on Mac, sent for Gemini context


class NextActionRequest(BaseModel):
    session_id: str
    platform: str
    tree: dict
    screenshot_b64: Optional[str] = None
    client_state: Optional[ClientState] = None
    last_result: Optional[LastResult] = None
    chat_message: Optional[str] = None  # Proactive user message from chat panel
