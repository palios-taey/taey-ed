# STATUS: FROZEN. Request models. Verified 2026-02-19. Do not modify.
"""
Pydantic request models for the Taey-Ed API.

Pydantic request/response shapes for the API.
"""

from pydantic import BaseModel, field_validator
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
    # str OR dict: discover_menu emits option dicts ({"text": ...}); the engine
    # extracts display text via _option_text. Accepting dicts lets per-box
    # solve_complex take ENUMERATED options directly — string-option dropdowns hide
    # their options inside the closed box, so the LLM must be GIVEN them or it
    # free-associates/rambles (operator live 2026-06-14). No 422 on dict options.
    options: Optional[List[Union[str, Dict]]] = None
    context: Optional[List[str]] = None
    image_descriptions: Optional[List[str]] = None
    has_text_field: bool = False
    screen_config: Optional[Dict] = None
    items: Optional[List[Dict]] = None
    screenshot_b64: Optional[str] = None
    # Top-K relevant chunks from the user's local KB, attached by the Mac's
    # send_to_llm handler so the answer grounds in THIS course's own
    # video/article content (INTENDED_FLOW §C). Forward ref — KBChunk is
    # defined below; resolved by the model_rebuild() call after it.
    relevant_kb_chunks: Optional[List["KBChunk"]] = None

    @field_validator("relevant_kb_chunks", mode="before")
    @classmethod
    def _drop_malformed_chunks(cls, v):
        """Chunks are auxiliary context — a malformed entry must NEVER 422
        the whole question (observed live 2026-06-11 14:26: course=unknown
        served a degenerate KB row, strict typing rejected the request, the
        quiz question's solve died). Filter bad entries loudly, keep good."""
        if not isinstance(v, list):
            return v
        good, dropped = [], 0
        for ch in v:
            if isinstance(ch, dict) and (ch.get("text") or "").strip():
                ch.setdefault("source_screen_type", "UNKNOWN")
                good.append(ch)
            else:
                dropped += 1
        if dropped:
            import logging
            logging.getLogger(__name__).warning(
                f"GenerateRequest: dropped {dropped} malformed relevant_kb_chunks "
                f"(kept {len(good)})"
            )
        return good


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
    bt_blackboard: Optional[dict] = None  # Bounded serializable Mac blackboard snapshot
    bt_find_all_results: Optional[dict] = None  # Stored find_all outputs with raw AX refs stripped


class KBChunk(BaseModel):
    """A relevant chunk retrieved from the user's local DeepTutor KB.

    The Mac app captures content during VIDEO/ARTICLE subtype screens, embeds via
    /api/v1/embed (Qwen3-Embedding-8B native 4096d), and stores (text,
    vector) pairs locally in DeepTutor (per Jesse 2026-05-12: NO truncation).
    At EXERCISE time, the Mac embeds the question, runs local similarity
    search, and attaches the top-K matching chunks to the consultation
    request as `relevant_kb_chunks`. The BT generator includes them in the
    Claude prompt as retrieved context.

    Per LAUNCH_PLAN v4 §4 Gap E and §0 user-sovereignty principle: the KB
    stays on the user's Mac. Only top-K relevant text chunks travel to the
    central server, never the whole KB.
    """
    source_screen_type: str  # canonical source subtype; current frozen Mac path still emits VIDEO/ARTICLE masters
    source_screen_id: Optional[str] = None  # opaque stable hash from the Mac
    captured_at: Optional[str] = None  # ISO-8601
    text: str  # the actual chunk text (≤1500 chars per chunk recommended)
    score: Optional[float] = None  # cosine similarity to query, 0..1
    kb_chunk_id: Optional[str] = None  # opaque local-only ID


# Resolve the KBChunk forward reference in GenerateRequest (defined above).
GenerateRequest.model_rebuild()


class NextActionRequest(BaseModel):
    session_id: str
    platform: str
    tree: dict
    screenshot_b64: Optional[str] = None
    client_state: Optional[ClientState] = None
    last_result: Optional[LastResult] = None
    chat_message: Optional[str] = None  # Proactive user message from chat panel
    # Top-K relevant chunks from the user's local DeepTutor KB. Mac runs
    # similarity search locally before posting the consultation request.
    # Max 5 chunks per request to keep prompt size bounded.
    relevant_kb_chunks: Optional[List[KBChunk]] = None
