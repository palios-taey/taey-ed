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
from spark_v2.learning import invalidation
from spark_v2.learning.cache import load_cached_bt
from spark_v2.learning.outcome_log import log_event, log_graduation_event, log_outcome
from spark_v2.recovery import (
    capture_user_guidance,
    create_request as create_recovery_request,
    find_active_recovery_request,
    get_metadata_status as get_recovery_metadata_status,
    poll_for_result as poll_for_recovery_result,
    validate_and_merge_recovery_result,
)
from spark_v2.tasks.consultation_request import request_consultation
from spark_v2.tasks.knowledge_loader import (
    graduate_active_recovery_entries,
    increment_meta_counter,
    is_first_touch,
    load_knowledge,
    load_provisional,
    record_failed_recovery_attempt,
    save_knowledge,
)
from spark_v2.tasks.prompt_codex import UNIVERSAL_LAYER_PATH, load_onboarding_messages, prune_ax_tree
from spark_v2.tasks.screen_signatures import compute_signature
from spark_v2.tasks.skeleton import extract_skeleton, hash_skeleton
from spark_v2.tasks.screen_type_util import get_master_category

CONSULT_DIR = Path("/tmp/taey-ed-consult-v2")
ONBOARDING_MESSAGES = load_onboarding_messages(str(UNIVERSAL_LAYER_PATH))


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


def _structural_hash(tree: object) -> str | None:
    if not isinstance(tree, dict) or not tree:
        return None
    pruned = prune_ax_tree(tree)
    if not isinstance(pruned, dict) or not pruned:
        return None
    return hash_skeleton(extract_skeleton(pruned))


def _video_poll_tree() -> dict:
    return {
        "type": "action",
        "action": "video_poll",
        "params": {},
    }


def _cache_short_circuit_consultation_id() -> str:
    return f"cache_short_circuit_{int(time.time())}"


def _cache_short_circuit_active(payload: dict, last_result: dict) -> tuple[bool, str, dict | None]:
    if _extract_active_consultation_id(payload):
        return False, "", None
    skeleton_hash = str(last_result.get("directive_skeleton_hash") or "")
    if not skeleton_hash:
        return False, "", None
    entry = load_cached_bt(payload.get("platform", "unknown"), skeleton_hash)
    if not isinstance(entry, dict):
        return False, skeleton_hash, None
    return str(entry.get("cache_class") or "") == "DETERMINISTIC_BT", skeleton_hash, entry


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


def _needs_screenshot_capture(payload: dict) -> bool:
    """True when we are about to spawn a fresh consultation but Mac has not yet sent a screenshot."""
    client_state = payload.get("client_state") or {}
    active_consultation_id = client_state.get("active_consultation_id")
    if active_consultation_id:
        return False
    screenshot_b64 = payload.get("screenshot_b64")
    if isinstance(screenshot_b64, str) and screenshot_b64.strip():
        return False
    return True


def _need_screenshot_directive() -> dict:
    return {
        "directive": "need_screenshot",
        "directive_id": _make_directive_id(),
        "reason": "spark requires visual ground truth before classification",
    }


def _prompt_payload_from_request(
    payload: dict,
    tier: int,
    chat_override: bool = False,
    cache_steering_entry: dict | None = None,
    cache_steering_hash: str | None = None,
) -> dict:
    prompt = {
        "platform": payload.get("platform"),
        "last_result": payload.get("last_result"),
        "chat_message": payload.get("chat_message"),
        "relevant_kb_chunks": payload.get("relevant_kb_chunks"),
        "client_state": payload.get("client_state"),
        "current_url": payload.get("current_url"),
        "tier": tier,
        "chat_override": chat_override,
    }
    if cache_steering_entry:
        prompt["cache_steering_entry"] = cache_steering_entry
        prompt["cache_steering_hash"] = cache_steering_hash
    return prompt


def _spawn_consultation(
    payload: dict,
    *,
    tier: int,
    metadata: dict,
    cache_steering_entry: dict | None = None,
    cache_steering_hash: str | None = None,
) -> dict:
    consult = request_consultation(
        platform=payload.get("platform", "unknown"),
        tree=payload.get("tree") or {},
        screenshot_b64=payload.get("screenshot_b64"),
        prompt_payload=_prompt_payload_from_request(
            payload,
            tier=tier,
            chat_override=bool(metadata.get("chat_override")),
            cache_steering_entry=cache_steering_entry,
            cache_steering_hash=cache_steering_hash,
        ),
        metadata=metadata,
    )
    return _consulting_directive(consult["consultation_id"], poll_interval=3.0)


def _load_consult_metadata(consultation_id: str | None) -> dict:
    if not consultation_id:
        return {}
    path = CONSULT_DIR / consultation_id / "metadata.json"
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text())
    except Exception:
        return {}


def _course_id(payload: dict) -> str:
    return str(((payload.get("client_state") or {}).get("course_id")) or "")


def _current_screen_type(payload: dict, last_result: dict | None = None) -> str:
    if isinstance(last_result, dict):
        screen = str(last_result.get("screen") or "").strip()
        if screen:
            return screen
    return "UNKNOWN"


def _log_execution_outcome(payload: dict, *, step2_validated: bool, wrong_answer_retry: bool, worker_fallback: bool) -> None:
    last_result = payload.get("last_result")
    if not isinstance(last_result, dict):
        return
    consultation_id = _extract_active_consultation_id(payload) or str(last_result.get("consultation_id") or "")
    consult_meta = _load_consult_metadata(consultation_id)
    tier = int(consult_meta.get("tier") or 0)
    screen_type = _current_screen_type(payload, last_result)
    skeleton_hash = str(last_result.get("directive_skeleton_hash") or _current_skeleton_hash(payload))
    log_outcome(
        payload.get("platform", "unknown"),
        screen_type,
        skeleton_hash,
        consultation_id,
        _course_id(payload),
        last_result.get("failed_bt") or {"action": last_result.get("action")},
        bool(last_result.get("success")),
        tier,
        wrong_answer_retry,
        worker_fallback,
        step2_validated,
        error=None if last_result.get("success") else str(last_result.get("action") or "failed"),
        fingerprint=str(last_result.get("directive_id") or ""),
    )


def _capture_guidance_if_present(payload: dict) -> None:
    last_result = payload.get("last_result")
    if not isinstance(last_result, dict):
        return
    user_response = str(last_result.get("user_response") or "").strip()
    if not user_response:
        return
    consultation_id = _extract_active_consultation_id(payload) or str(last_result.get("consultation_id") or "")
    capture_user_guidance(
        platform=payload.get("platform", "unknown"),
        consultation_id=consultation_id or "tier3",
        course_id=_course_id(payload),
        screen_type=_current_screen_type(payload, last_result),
        tree_hash=str(last_result.get("directive_skeleton_hash") or _current_tree_hash(payload)),
        guidance_text=user_response,
    )


def _dispatch_recovery_request(payload: dict, last_result: dict) -> dict:
    platform = payload.get("platform", "unknown")
    platform_data = load_knowledge(platform)
    provisional_data = load_provisional(platform)
    request_id = create_recovery_request(
        {
            "platform": platform,
            "platform_display_name": (platform_data.get("platform") or {}).get("display_name") or platform,
            "platform_url": _platform_url_hint(platform_data, payload),
            "screen_type": _current_screen_type(payload, last_result),
            "tier1_timestamp": _now(),
            "attempt_count": 2,
            "course_id": _course_id(payload),
            "tree": payload.get("tree") or {},
            "vision_extraction": "screenshot supplied in consult dir",
            "mismatch_hypothesis": "pending external recovery research",
            "failed_bt": last_result.get("failed_bt") or {},
            "bt_debug_tail": last_result.get("bt_debug_tail") or "",
            "knowledge": platform_data,
            "provisional": provisional_data,
        }
    )
    increment_meta_counter(platform, "recovery_consults_total")
    log_event(
        platform,
        event_kind="tier2_dispatched",
        screen_type=_current_screen_type(payload, last_result),
        skeleton_hash=str(last_result.get("directive_skeleton_hash") or _current_skeleton_hash(payload)),
        consultation_id=_extract_active_consultation_id(payload) or "",
        course_id=_course_id(payload),
        payload={"request_id": request_id},
    )
    return {
        "directive": "wait",
        "directive_id": _make_directive_id(),
        "seconds": 20.0,
        "reason": f"recovery in progress ({request_id})",
        "_recovery_request_id": request_id,
    }


def _platform_url_hint(platform_data: dict, payload: dict) -> str:
    current_url = payload.get("current_url")
    if isinstance(current_url, str) and current_url.strip():
        return current_url
    url_pattern = str((platform_data.get("platform") or {}).get("url_pattern") or "").strip()
    if url_pattern:
        if "://" in url_pattern:
            return url_pattern
        return f"https://{url_pattern}"
    platform_name = str((platform_data.get("platform") or {}).get("name") or "").strip()
    if platform_name:
        return platform_name
    return str(payload.get("platform") or "unknown")


def _discovery_started_directive(payload: dict, request_id: str) -> dict:
    current_hash = _current_tree_hash(payload)
    platform = str(payload.get("platform") or "this").replace("_", " ").strip() or "this"
    display_name = " ".join(part.capitalize() for part in platform.split())
    onboarding_message = ONBOARDING_MESSAGES["onboarding_message"].replace(
        "{PLATFORM_DISPLAY_NAME}",
        display_name,
    )
    discovery_message = ONBOARDING_MESSAGES["discovery_in_progress_message"]
    return {
        "directive": "user_input_needed",
        "directive_id": _make_directive_id(),
        "screen_type": "ONBOARDING_DISCOVERY",
        "screen": "platform_discovery",
        "tree_hash": current_hash,
        "reason": "first_touch_discovery_started",
        "message": discovery_message,
        "chat_messages": [
            _chat_message(onboarding_message),
            _chat_message(discovery_message),
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
    if _needs_screenshot_capture(payload):
        return _need_screenshot_directive()
    return _spawn_consultation(
        payload,
        tier=3,
        metadata={
            "tier": 3,
            "chat_override": True,
            "screen_type_hint": "UNKNOWN",
        },
    )


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
    _capture_guidance_if_present(payload)

    expected_next = last_result.get("directive_expected_next") or []
    if success and any(get_master_category(item) == "EXERCISE" for item in expected_next):
        prior_skeleton_hash = last_result.get("directive_skeleton_hash")
        if current_master == "EXERCISE" and prior_skeleton_hash and prior_skeleton_hash == current_skeleton_hash:
            _log_execution_outcome(payload, step2_validated=False, wrong_answer_retry=True, worker_fallback=False)
            return _user_input_directive(
                message="The action appears to have stayed on the same exercise screen.",
                screen_type="EXERCISE",
                tree_hash=current_tree_hash,
                reason="wrong_answer_or_same_exercise",
            )

    if success and continue_loop:
        return None

    if success and before_hash != after_hash and not continue_loop:
        _log_execution_outcome(payload, step2_validated=True, wrong_answer_retry=False, worker_fallback=False)
        consultation_id = _extract_active_consultation_id(payload) or str(last_result.get("consultation_id") or "")
        cache_short_circuit, skeleton_hash, cache_entry = _cache_short_circuit_active(payload, last_result)
        if cache_short_circuit:
            knowledge = load_knowledge(payload.get("platform", "unknown"))
            invalidation.on_post_promotion_success(skeleton_hash, payload.get("platform", "unknown"), knowledge)
            save_knowledge(payload.get("platform", "unknown"), knowledge)
            log_event(
                payload.get("platform", "unknown"),
                event_kind="cache_validation_success",
                screen_type=_current_screen_type(payload, last_result),
                skeleton_hash=skeleton_hash,
                consultation_id=consultation_id or _cache_short_circuit_consultation_id(),
                course_id=_course_id(payload),
                payload={"cache_class": (cache_entry or {}).get("cache_class")},
            )
        graduation = graduate_active_recovery_entries(payload.get("platform", "unknown"), consultation_id or "validated")
        if graduation.get("graduated_entry_ids"):
            log_graduation_event(
                payload.get("platform", "unknown"),
                str(last_result.get("directive_skeleton_hash") or current_skeleton_hash),
                consultation_id or "validated",
                {
                    "screen_type": _current_screen_type(payload, last_result),
                    "course_id": _course_id(payload),
                    "graduated_entry_ids": graduation["graduated_entry_ids"],
                },
            )
        return None

    action_label = str(last_result.get("action", ""))
    if success and before_hash == after_hash and not continue_loop:
        _log_execution_outcome(payload, step2_validated=False, wrong_answer_retry=False, worker_fallback=False)
        return _user_input_directive(
            message="The last action did not change the screen.",
            screen_type=last_result.get("screen", "UNKNOWN"),
            tree_hash=current_tree_hash,
            reason="tree_unchanged_after_success",
        )

    if not success and "tree_unchanged" in action_label:
        _log_execution_outcome(payload, step2_validated=False, wrong_answer_retry=False, worker_fallback=False)
        return _user_input_directive(
            message="The last action left the screen unchanged.",
            screen_type=last_result.get("screen", "UNKNOWN"),
            tree_hash=current_tree_hash,
            reason="tree_unchanged_failure",
        )

    if not success and last_result.get("failed_bt"):
        _log_execution_outcome(payload, step2_validated=False, wrong_answer_retry=False, worker_fallback=False)
        cache_short_circuit, skeleton_hash, cache_entry = _cache_short_circuit_active(payload, last_result)
        if cache_short_circuit:
            knowledge = load_knowledge(payload.get("platform", "unknown"))
            invalidation.on_post_promotion_failure(skeleton_hash, payload.get("platform", "unknown"), knowledge)
            save_knowledge(payload.get("platform", "unknown"), knowledge)
            log_event(
                payload.get("platform", "unknown"),
                event_kind="cache_validation_failure",
                screen_type=_current_screen_type(payload, last_result),
                skeleton_hash=skeleton_hash,
                consultation_id=_extract_active_consultation_id(payload)
                or str(last_result.get("consultation_id") or _cache_short_circuit_consultation_id()),
                course_id=_course_id(payload),
                payload={"cache_class": (cache_entry or {}).get("cache_class")},
            )
        record_failed_recovery_attempt(
            payload.get("platform", "unknown"),
            _extract_active_consultation_id(payload) or str(last_result.get("consultation_id") or "failed"),
        )
        return None

    return None


def step_2_7_polling_completion(payload: dict) -> dict | None:
    # Ported from v7 server.py:535-565 and 04_VIDEO_POLL_ARCHAEOLOGY.md §A2.
    # Polling completion is server-owned, but 2026-05-17 Chrome tab-strip
    # memory-usage label churn proved Mac tree_hash can change with no meaningful
    # content change. Compare pruned structural hashes first; only fall back to
    # raw Mac hashes when the prior after_tree is unavailable. The old v7
    # Escape+wait close assumption does not apply to current Khan; when the tree
    # meaningfully changes, fall through so Step 5 can classify the new state.
    # 2026-05-17 live Khan evidence also showed Step 4 lenient polling continuity
    # masking Step 5 after completion, so Step 2.7 is now the sole owner of
    # equal-vs-different polling-state routing.
    last_result = payload.get("last_result")
    if not isinstance(last_result, dict):
        return None
    if not bool(last_result.get("continue_loop")):
        return None
    if not last_result.get("tree_hash_before") or not last_result.get("tree_hash_after"):
        return None

    current_structural_hash = _structural_hash(payload.get("tree"))
    prior_structural_hash = _structural_hash(last_result.get("after_tree"))
    if current_structural_hash and prior_structural_hash:
        changed = current_structural_hash != prior_structural_hash
    else:
        changed = last_result.get("tree_hash_before") != last_result.get("tree_hash_after")

    if not changed:
        return {
            "directive": "execute_tree",
            "directive_id": _make_directive_id(),
            "consultation_id": _extract_active_consultation_id(payload),
            "tree": _video_poll_tree(),
            "screen": last_result.get("screen", "UNKNOWN"),
            "expected_next": [],
            "extract": None,
            "chat_messages": [],
            "skeleton_hash": str(last_result.get("directive_skeleton_hash") or ""),
        }
    if changed:
        return None
    return None


def step_3_failure_retry(payload: dict) -> dict | None:
    last_result = payload.get("last_result")
    if not isinstance(last_result, dict):
        return None
    if last_result.get("success") or not last_result.get("failed_bt"):
        return None
    consultation_id = _extract_active_consultation_id(payload) or str(last_result.get("consultation_id") or "")
    consult_meta = _load_consult_metadata(consultation_id)
    prior_tier = int(consult_meta.get("tier") or 0)
    if _needs_screenshot_capture(payload):
        return _need_screenshot_directive()
    if prior_tier >= 1:
        return _dispatch_recovery_request(payload, last_result)
    return _spawn_consultation(
        payload,
        tier=1,
        metadata={
            "tier": 1,
            "screen_type_hint": last_result.get("screen", "UNKNOWN"),
            "failure_reason": str(last_result.get("action", "bt_failure")),
        },
    )


def step_4_signature_match(payload: dict) -> dict | None:
    # Phase F cache routing only. Polling continuity is owned exclusively by
    # Step 2.7 so a meaningful post-poll tree change can reach Step 5.
    platform = payload.get("platform", "unknown")
    tree = payload.get("tree") or {}
    skeleton = extract_skeleton(tree)
    skeleton_hash = hash_skeleton(skeleton)
    signature = compute_signature(tree)
    if signature and signature != skeleton_hash:
        skeleton_hash = signature

    entry = load_cached_bt(platform, skeleton_hash)
    if not isinstance(entry, dict):
        return None

    cache_class = str(entry.get("cache_class") or "")
    screen_type = str(entry.get("screen_type") or "UNKNOWN")
    if cache_class == "DETERMINISTIC_BT":
        consultation_id = _cache_short_circuit_consultation_id()
        log_event(
            platform,
            event_kind="cache_hit",
            screen_type=screen_type,
            skeleton_hash=skeleton_hash,
            consultation_id=consultation_id,
            course_id=_course_id(payload),
            payload={"cache_class": cache_class, "cost_usd": 0.0},
        )
        return {
            "directive": "execute_tree",
            "directive_id": _make_directive_id(),
            "consultation_id": consultation_id,
            "tree": entry.get("bt", {"type": "sequence", "children": []}),
            "screen": screen_type,
            "expected_next": entry.get("expected_next", []),
            "extract": None,
            "chat_messages": [],
            "skeleton_hash": skeleton_hash,
        }

    if cache_class == "PROCEDURAL_TEMPLATE":
        if _needs_screenshot_capture(payload):
            return _need_screenshot_directive()
        log_event(
            platform,
            event_kind="cache_hit",
            screen_type=screen_type,
            skeleton_hash=skeleton_hash,
            consultation_id="",
            course_id=_course_id(payload),
            payload={"cache_class": cache_class, "cost_usd": None},
        )
        return _spawn_consultation(
            payload,
            tier=0,
            metadata={
                "tier": 0,
                "screen_type_hint": screen_type,
                "cache_class": cache_class,
            },
            cache_steering_entry=entry,
            cache_steering_hash=skeleton_hash,
        )

    return None


def step_5_knowledge_gate_and_classify(payload: dict) -> dict | None:
    platform = payload.get("platform", "unknown")
    platform_data = load_knowledge(platform)
    provisional_data = load_provisional(platform)
    current_hash = _current_tree_hash(payload)

    if platform == "unknown":
        if _needs_screenshot_capture(payload):
            return _need_screenshot_directive()
        return _spawn_consultation(
            payload,
            tier=0,
            metadata={
                "tier": 0,
                "screen_type_hint": "UNKNOWN",
            },
        )

    if is_first_touch(platform_data) and provisional_data is None:
        active_request = find_active_discovery_request(platform)
        if not active_request:
            platform_type = str(
                (payload.get("client_state") or {}).get("platform_type")
                or (platform_data.get("platform") or {}).get("platform_type")
                or ""
            )
            request_id = create_request(
                {
                    "platform": platform,
                    "platform_url": _platform_url_hint(platform_data, payload),
                    "platform_type": platform_type,
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

    active_recovery = find_active_recovery_request(platform)
    if active_recovery:
        recovery_status = get_recovery_metadata_status(active_recovery)
        recovery_result = poll_for_recovery_result(active_recovery)
        if recovery_result is None and recovery_status == "pending":
            return {
                "directive": "wait",
                "directive_id": _make_directive_id(),
                "seconds": 20.0,
                "reason": f"recovery in progress ({active_recovery})",
                "_recovery_request_id": active_recovery,
            }
        if recovery_result is None and recovery_status == "harvested":
            return _user_input_directive(
                message="Recovery harvested a result, but the result artifact is missing.",
                screen_type="RECOVERY",
                tree_hash=current_hash,
                reason="recovery_missing_result",
                extra={"_recovery_request_id": active_recovery},
            )
        if recovery_status in {"auth_required", "malformed_output", "timeout"}:
            return _user_input_directive(
                message=f"Recovery research could not complete automatically (status={recovery_status}).",
                screen_type="RECOVERY",
                tree_hash=current_hash,
                reason="recovery_failed",
                extra={"_recovery_request_id": active_recovery},
            )
        if recovery_result is not None:
            ok, errors = validate_and_merge_recovery_result(recovery_result, platform, active_recovery)
            if not ok:
                detail = errors[0] if errors else "unknown validation error"
                return _user_input_directive(
                    message=f"Recovery research returned invalid data. First error: {detail}",
                    screen_type="RECOVERY",
                    tree_hash=current_hash,
                    reason="recovery_invalid_result",
                    extra={"_recovery_request_id": active_recovery},
                )
        elif recovery_status not in {"pending", "harvested"}:
            return _user_input_directive(
                message=f"Recovery is in an unexpected state ({recovery_status or 'unknown'}).",
                screen_type="RECOVERY",
                tree_hash=current_hash,
                reason="recovery_unexpected_status",
                extra={"_recovery_request_id": active_recovery},
            )

    if _needs_screenshot_capture(payload):
        return _need_screenshot_directive()
    return _spawn_consultation(
        payload,
        tier=0,
        metadata={
            "tier": 0,
            "screen_type_hint": "UNKNOWN",
        },
    )


def build_unknown_directive(payload: dict) -> dict:
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
    return build_unknown_directive(payload)
