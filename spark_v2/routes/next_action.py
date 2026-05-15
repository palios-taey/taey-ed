"""Phase B scaffold for the spark_v2 /next_action pipeline."""

from __future__ import annotations

from pathlib import Path
import json
import time
import uuid

CONSULT_DIR = Path("/tmp/taey-ed-consult-v2")


def _make_directive_id() -> str:
    return f"d-{uuid.uuid4().hex[:8]}"


def _extract_active_consultation_id(payload: dict) -> str | None:
    client_state = payload.get("client_state")
    if isinstance(client_state, dict):
        value = client_state.get("active_consultation_id")
        if isinstance(value, str) and value:
            return value
    value = payload.get("active_consultation_id")
    if isinstance(value, str) and value:
        return value
    return None


def step_0_chat_override(payload: dict) -> dict | None:
    # TODO Phase C1: preserve chat_message urgent-override semantics from the
    # current directive model without importing legacy spark code.
    _ = payload
    return None


def step_1_active_consultation_poll(payload: dict) -> dict | None:
    # TODO Phase C2: preserve active-consultation polling and worker-fallback
    # semantics, including _worker_fallback -> user_input_needed routing.
    consultation_id = _extract_active_consultation_id(payload)
    if not consultation_id:
        return None

    consult_dir = CONSULT_DIR / consultation_id
    meta_path = consult_dir / "metadata.json"
    response_path = consult_dir / "response.json"

    if not meta_path.exists():
        return {
            "directive": "user_input_needed",
            "directive_id": _make_directive_id(),
            "screen_type": "UNKNOWN",
            "message": "Active consultation metadata is missing.",
            "consultation_id": consultation_id,
            "todo": "Phase C2",
        }

    try:
        metadata = json.loads(meta_path.read_text())
    except Exception:
        return {
            "directive": "user_input_needed",
            "directive_id": _make_directive_id(),
            "screen_type": "UNKNOWN",
            "message": "Active consultation metadata is unreadable.",
            "consultation_id": consultation_id,
            "todo": "Phase C2",
        }

    if response_path.exists():
        try:
            response = json.loads(response_path.read_text())
        except Exception:
            return {
                "directive": "user_input_needed",
                "directive_id": _make_directive_id(),
                "screen_type": "UNKNOWN",
                "message": "Consultation response exists but is unreadable.",
                "consultation_id": consultation_id,
                "todo": "Phase C2",
            }
        if response.get("_worker_fallback"):
            return {
                "directive": "user_input_needed",
                "directive_id": _make_directive_id(),
                "screen_type": "UNKNOWN",
                "message": response.get("_worker_failure_reason", "worker_fallback"),
                "consultation_id": consultation_id,
                "todo": "Phase C2",
            }
        return {
            "directive": "execute_tree",
            "directive_id": _make_directive_id(),
            "consultation_id": consultation_id,
            "tree": response.get("tree", {"type": "sequence", "children": []}),
            "screen_type": response.get("screen_type", "UNKNOWN"),
            "expected_next": response.get("expected_next", []),
            "extract": response.get("extract"),
            "todo": "Phase C2",
        }

    return {
        "directive": "consulting",
        "directive_id": _make_directive_id(),
        "consultation_id": consultation_id,
        "status": metadata.get("status", "pending"),
        "poll_after_seconds": 2.0,
        "todo": "Phase C2",
    }


def step_2_validate_previous_action(payload: dict) -> dict | None:
    # TODO Phase C3: validate prior action results, wrong-answer detection,
    # and tree-change semantics.
    _ = payload
    return None


def step_2_7_polling_completion(payload: dict) -> dict | None:
    # TODO Phase C4: implement polling completion based on tree-change signal.
    _ = payload
    return None


def step_3_failure_retry(payload: dict) -> dict | None:
    # TODO Phase C5: implement Tier 1 reconsult with failure context.
    _ = payload
    return None


def step_4_signature_match(payload: dict) -> dict | None:
    # TODO Phase C6: implement signature/cache matching and screen-class routing.
    _ = payload
    return None


def step_5_knowledge_gate_and_classify(payload: dict) -> dict | None:
    # TODO Phase C7: implement knowledge gate, classify path, and BT build.
    _ = payload
    return None


def build_unknown_placeholder(payload: dict) -> dict:
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    return {
        "directive": "user_input_needed",
        "directive_id": _make_directive_id(),
        "screen_type": "UNKNOWN",
        "tree": {"type": "sequence", "children": []},
        "expected_next": [],
        "extract": None,
        "todo": "Phase B scaffold; implement Phase C1-C7.",
        "generated_at": now,
        "observed_keys": sorted(payload.keys()),
    }


def decide_next_action(payload: dict) -> dict:
    for step in (
        step_0_chat_override,
        step_1_active_consultation_poll,
        step_2_validate_previous_action,
        step_2_7_polling_completion,
        step_3_failure_retry,
        step_4_signature_match,
        step_5_knowledge_gate_and_classify,
    ):
        result = step(payload)
        if result is not None:
            return result
    return build_unknown_placeholder(payload)
