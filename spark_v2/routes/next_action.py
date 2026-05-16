"""Decision pipeline for spark_v2."""

from __future__ import annotations

import hashlib
import json
import time
import uuid
from pathlib import Path

from spark_v2.discovery import (
    create_request,
    find_active_discovery_request,
    get_metadata_status,
    poll_for_result,
    validate_and_promote_to_provisional,
)
from spark_v2.tasks.consultation_request import request_consultation
from spark_v2.tasks.knowledge_loader import is_first_touch, load_knowledge, load_provisional
from spark_v2.tasks.skeleton import extract_skeleton, hash_skeleton
from spark_v2.tasks.screen_type_util import get_master_category

CONSULT_DIR = Path("/tmp/taey-ed-consult-v2")
ONBOARDING_MESSAGE = (
    "Hi, I'm Taey. I'm looking forward to helping you with this course. This is a new "
    "platform for me, so I might need your help on the first video and first ... screen, "
    "but after that, I'll be good to go on my own."
)
DISCOVERY_IN_PROGRESS_MESSAGE = (
    "I am researching this platform now, this takes a few minutes. While you wait, you can "
    "help me with the first screen if you would like."
)


def _make_directive_id() -> str:
    return f"d-{uuid.uuid4().hex[:8]}"


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _chat_message(text: str, msg_type: str = "question") -> dict:
    return {
        "sender": "system",
        "text": text,
        "msg_type": msg_type,
        "timestamp": time.time(),
    }


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


def _tree_hash(tree: dict) -> str:
    blob = json.dumps(tree or {}, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _current_tree_hash(payload: dict) -> str:
    tree = payload.get("tree") or {}
    return _tree_hash(tree)


def _current_skeleton_hash(payload: dict) -> str:
    return hash_skeleton(extract_skeleton(payload.get("tree") or {}))


def _user_input_directive(
    *,
    message: str,
    screen_type: str,
    tree_hash: str,
    reason: str,
    extra: dict | None = None,
) -> dict:
    response = {
        "directive": "user_input_needed",
        "directive_id": _make_directive_id(),
        "screen_type": screen_type,
        "tree_hash": tree_hash,
        "reason": reason,
        "message": message,
        "chat_messages": [_chat_message(message)],
    }
    if extra:
        response.update(extra)
    return response


def _consulting_directive(consultation_id: str, poll_interval: float = 3.0) -> dict:
    return {
        "directive": "consulting",
        "directive_id": _make_directive_id(),
        "consultation_id": consultation_id,
        "poll_interval": poll_interval,
    }


def _prompt_payload_from_request(payload: dict, tier: int, chat_override: bool = False) -> dict:
    return {
        "platform": payload.get("platform"),
        "last_result": payload.get("last_result"),
        "chat_message": payload.get("chat_message"),
        "relevant_kb_chunks": payload.get("relevant_kb_chunks"),
        "client_state": payload.get("client_state"),
        "current_url": payload.get("current_url"),
        "tier": tier,
        "chat_override": chat_override,
    }


def _platform_url_hint(platform: str, payload: dict) -> str:
    known = {
        "khan_academy": "https://www.khanacademy.org",
    }
    if platform in known:
        return known[platform]
    current_url = payload.get("current_url")
    if isinstance(current_url, str) and current_url.strip():
        return current_url
    return platform


def _discovery_started_directive(payload: dict, request_id: str) -> dict:
    current_hash = _current_tree_hash(payload)
    return {
        "directive": "user_input_needed",
        "directive_id": _make_directive_id(),
        "screen_type": "ONBOARDING_DISCOVERY",
        "screen": "platform_discovery",
        "tree_hash": current_hash,
        "reason": "first_touch_discovery_started",
        "message": DISCOVERY_IN_PROGRESS_MESSAGE,
        "chat_messages": [
            _chat_message(ONBOARDING_MESSAGE),
            _chat_message(DISCOVERY_IN_PROGRESS_MESSAGE),
        ],
        "_discovery_request_id": request_id,
    }


def step_0_chat_override(payload: dict) -> dict | None:
    chat_message = payload.get("chat_message")
    if not isinstance(chat_message, str) or not chat_message.strip():
        last_result = payload.get("last_result")
        if isinstance(last_result, dict):
            user_response = last_result.get("user_response")
            if isinstance(user_response, str) and user_response.strip():
                chat_message = user_response
                payload = dict(payload)
                payload["chat_message"] = user_response
    if not isinstance(chat_message, str) or not chat_message.strip():
        return None
    consult = request_consultation(
        platform=payload.get("platform", "unknown"),
        tree=payload.get("tree") or {},
        screenshot_b64=payload.get("screenshot_b64"),
        prompt_payload=_prompt_payload_from_request(payload, tier=3, chat_override=True),
        metadata={
            "tier": 3,
            "chat_override": True,
            "screen_type_hint": "UNKNOWN",
        },
    )
    return _consulting_directive(consult["consultation_id"], poll_interval=3.0)


def step_1_active_consultation_poll(payload: dict) -> dict | None:
    consultation_id = _extract_active_consultation_id(payload)
    if not consultation_id:
        return None

    consult_dir = CONSULT_DIR / consultation_id
    meta_path = consult_dir / "metadata.json"
    response_path = consult_dir / "response.json"
    current_hash = _current_tree_hash(payload)

    if not meta_path.exists():
        return _user_input_directive(
            message="Active consultation metadata is missing.",
            screen_type="UNKNOWN",
            tree_hash=current_hash,
            reason="active_consultation_missing",
            extra={"consultation_id": consultation_id},
        )

    try:
        metadata = json.loads(meta_path.read_text())
    except Exception:
        return _user_input_directive(
            message="Active consultation metadata is unreadable.",
            screen_type="UNKNOWN",
            tree_hash=current_hash,
            reason="active_consultation_bad_metadata",
            extra={"consultation_id": consultation_id},
        )

    if response_path.exists():
        try:
            response = json.loads(response_path.read_text())
        except Exception:
            return _user_input_directive(
                message="Consultation response exists but is unreadable.",
                screen_type=metadata.get("screen_type_hint", "UNKNOWN"),
                tree_hash=current_hash,
                reason="active_consultation_bad_response",
                extra={"consultation_id": consultation_id},
            )

        if response.get("_worker_fallback"):
            return _user_input_directive(
                message=response.get("_worker_failure_reason", "worker_fallback"),
                screen_type=response.get("screen_type", metadata.get("screen_type_hint", "UNKNOWN")),
                tree_hash=current_hash,
                reason="worker_fallback",
                extra={"consultation_id": consultation_id},
            )

        return {
            "directive": "execute_tree",
            "directive_id": _make_directive_id(),
            "consultation_id": consultation_id,
            "tree": response.get("tree", {"type": "sequence", "children": []}),
            "screen": response.get("screen_type", "UNKNOWN"),
            "expected_next": response.get("expected_next", []),
            "extract": response.get("extract"),
            "chat_messages": response.get("chat_messages", []),
        }

    if metadata.get("status") == "pending":
        return _consulting_directive(consultation_id, poll_interval=3.0)

    return _user_input_directive(
        message=f"Consultation stalled with status={metadata.get('status', 'unknown')}.",
        screen_type=metadata.get("screen_type_hint", "UNKNOWN"),
        tree_hash=current_hash,
        reason="active_consultation_stalled",
        extra={"consultation_id": consultation_id},
    )


def step_2_validate_previous_action(payload: dict) -> dict | None:
    last_result = payload.get("last_result")
    if not isinstance(last_result, dict):
        return None

    success = bool(last_result.get("success"))
    continue_loop = bool(last_result.get("continue_loop"))
    before_hash = last_result.get("tree_hash_before")
    after_hash = last_result.get("tree_hash_after")
    current_tree_hash = _current_tree_hash(payload)
    current_skeleton_hash = _current_skeleton_hash(payload)
    current_master = get_master_category(last_result.get("screen"))

    expected_next = last_result.get("directive_expected_next") or []
    if success and any(get_master_category(item) == "EXERCISE" for item in expected_next):
        prior_skeleton_hash = last_result.get("directive_skeleton_hash")
        if current_master == "EXERCISE" and prior_skeleton_hash and prior_skeleton_hash == current_skeleton_hash:
            return _user_input_directive(
                message="The action appears to have stayed on the same exercise screen.",
                screen_type="EXERCISE",
                tree_hash=current_tree_hash,
                reason="wrong_answer_or_same_exercise",
            )

    if success and continue_loop:
        return {
            "directive": "wait",
            "directive_id": _make_directive_id(),
            "seconds": 5.0,
            "reason": "polling_continue_loop",
            "todo": "Phase C4",
        }

    if success and before_hash != after_hash and not continue_loop:
        return None

    action_label = str(last_result.get("action", ""))
    if success and before_hash == after_hash and not continue_loop:
        return _user_input_directive(
            message="The last action did not change the screen.",
            screen_type=last_result.get("screen", "UNKNOWN"),
            tree_hash=current_tree_hash,
            reason="tree_unchanged_after_success",
        )

    if not success and "tree_unchanged" in action_label:
        return _user_input_directive(
            message="The last action left the screen unchanged.",
            screen_type=last_result.get("screen", "UNKNOWN"),
            tree_hash=current_tree_hash,
            reason="tree_unchanged_failure",
        )

    if not success and last_result.get("failed_bt"):
        return None

    return None


def step_2_7_polling_completion(payload: dict) -> dict | None:
    _ = payload
    # TODO Phase C4: replace placeholder with tree-change completion handling.
    return None


def step_3_failure_retry(payload: dict) -> dict | None:
    last_result = payload.get("last_result")
    if not isinstance(last_result, dict):
        return None
    if last_result.get("success") or not last_result.get("failed_bt"):
        return None

    consult = request_consultation(
        platform=payload.get("platform", "unknown"),
        tree=payload.get("tree") or {},
        screenshot_b64=payload.get("screenshot_b64"),
        prompt_payload=_prompt_payload_from_request(payload, tier=1),
        metadata={
            "tier": 1,
            "screen_type_hint": last_result.get("screen", "UNKNOWN"),
            "failure_reason": str(last_result.get("action", "bt_failure")),
        },
    )
    return _consulting_directive(consult["consultation_id"], poll_interval=3.0)


def step_4_signature_match(payload: dict) -> dict | None:
    _ = payload
    # TODO Phase F: wire deterministic and procedural cache routing.
    return None


def step_5_knowledge_gate_and_classify(payload: dict) -> dict | None:
    platform = payload.get("platform", "unknown")
    platform_data = load_knowledge(platform)
    provisional_data = load_provisional(platform)
    current_hash = _current_tree_hash(payload)

    if is_first_touch(platform_data) and provisional_data is None:
        active_request = find_active_discovery_request(platform)
        if not active_request:
            request_id = create_request(
                {
                    "platform": platform,
                    "platform_url": _platform_url_hint(platform, payload),
                    "platform_type": str(
                        (payload.get("client_state") or {}).get("platform_type") or "MOOC"
                    ),
                    "any_context": str(payload.get("chat_message") or ""),
                }
            )
            return _discovery_started_directive(payload, request_id)

        status = get_metadata_status(active_request)
        result = poll_for_result(active_request)

        if result is None and status == "pending":
            return {
                "directive": "wait",
                "directive_id": _make_directive_id(),
                "seconds": 20.0,
                "reason": f"discovery in progress ({active_request})",
                "_discovery_request_id": active_request,
            }

        if result is None and status == "harvested":
            return _user_input_directive(
                message=(
                    "Discovery reported a harvested result, but the result artifact is missing. "
                    "Could you help me with the first screen while this is corrected?"
                ),
                screen_type="ONBOARDING_DISCOVERY",
                tree_hash=current_hash,
                reason="discovery_missing_result",
                extra={"_discovery_request_id": active_request},
            )

        if status in {"auth_required", "malformed_output", "timeout"}:
            return _user_input_directive(
                message=(
                    "I could not finish researching this platform automatically. "
                    f"Discovery status was {status}. Could you help me with the first screen?"
                ),
                screen_type="ONBOARDING_DISCOVERY",
                tree_hash=current_hash,
                reason="discovery_failed",
                extra={"_discovery_request_id": active_request},
            )

        if result is not None:
            ok, errors = validate_and_promote_to_provisional(result, platform, active_request)
            if not ok:
                detail = errors[0] if errors else "unknown validation error"
                return _user_input_directive(
                    message=f"Discovery research returned invalid data. First error: {detail}",
                    screen_type="ONBOARDING_DISCOVERY",
                    tree_hash=current_hash,
                    reason="discovery_invalid_result",
                    extra={"_discovery_request_id": active_request},
                )
            provisional_data = load_provisional(platform)
        elif status not in {"pending", "harvested"}:
            return _user_input_directive(
                message=(
                    "Discovery is in an unexpected state and could not continue automatically. "
                    f"Current status is {status or 'unknown'}."
                ),
                screen_type="ONBOARDING_DISCOVERY",
                tree_hash=current_hash,
                reason="discovery_unexpected_status",
                extra={"_discovery_request_id": active_request},
            )

    consult = request_consultation(
        platform=platform,
        tree=payload.get("tree") or {},
        screenshot_b64=payload.get("screenshot_b64"),
        prompt_payload=_prompt_payload_from_request(payload, tier=0),
        metadata={
            "tier": 0,
            "screen_type_hint": "UNKNOWN",
        },
    )
    return _consulting_directive(consult["consultation_id"], poll_interval=3.0)


def build_unknown_placeholder(payload: dict) -> dict:
    return _user_input_directive(
        message="No safe next action could be determined.",
        screen_type="UNKNOWN",
        tree_hash=_current_tree_hash(payload),
        reason="no_matching_step",
        extra={"generated_at": _now()},
    )


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
