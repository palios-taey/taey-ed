"""Claude CLI BT generation for spark_v2."""

from __future__ import annotations

import base64
import json
import logging
import subprocess
import time
from pathlib import Path

from spark_v2.tasks.knowledge_loader import load_knowledge, load_provisional
from spark_v2.tasks.build_consultation_prompt import build_consultation_prompt

logger = logging.getLogger(__name__)
prompt_logger = logging.getLogger("spark_v2.tasks.prompt_codex")

CONSULT_DIR = Path("/tmp/taey-ed-consult-v2")
DEFAULT_MODEL = "claude-opus-4-7"
DEFAULT_TIMEOUT_S = 180.0
DEFAULT_MAX_BUDGET_USD = 2.5
BT_RESPONSE_REQUIRED = [
    "screen_type",
    "tree",
    "expected_next",
    "extract",
    "target_source",
    "why_safe",
    "confidence",
]
BT_TREE_ROOT_TYPES = {"sequence", "action"}
BT_CONFIDENCE_VALUES = {"high", "medium", "low"}


class BTGenerationError(RuntimeError):
    """Raised when BT generation fails."""


def _load_consult_context(consultation_id: str) -> tuple[dict, dict, dict, Path]:
    consult_dir = CONSULT_DIR / consultation_id
    if not consult_dir.exists():
        raise BTGenerationError(f"consultation directory missing: {consult_dir}")
    try:
        tree = json.loads((consult_dir / "tree.json").read_text())
        metadata = json.loads((consult_dir / "metadata.json").read_text())
        prompt_payload = json.loads((consult_dir / "prompt.json").read_text())
    except Exception as exc:
        raise BTGenerationError(f"consultation files unreadable: {exc}") from exc
    return tree, metadata, prompt_payload, consult_dir


def _extract_json_object(text: str) -> str:
    raw = text.strip()
    if raw.startswith("```"):
        lines = raw.splitlines()
        if lines and lines[-1].strip() == "```":
            raw = "\n".join(lines[1:-1]).strip()
        else:
            raw = "\n".join(lines[1:]).strip()
    start = raw.find("{")
    if start < 0:
        raise BTGenerationError("no JSON object found in Claude output")
    depth = 0
    in_string = False
    escape = False
    for index in range(start, len(raw)):
        char = raw[index]
        if escape:
            escape = False
            continue
        if in_string:
            if char == "\\":
                escape = True
            elif char == "\"":
                in_string = False
            continue
        if char == "\"":
            in_string = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return raw[start:index + 1]
    raise BTGenerationError("unbalanced JSON object in Claude output")


def _validate_bt(payload: dict) -> list[str]:
    warnings: list[str] = []
    for key in BT_RESPONSE_REQUIRED:
        if key not in payload:
            raise BTGenerationError(f"missing BT response key: {key}")

    tree = payload.get("tree")
    if not isinstance(tree, dict):
        raise BTGenerationError("BT tree must be a JSON object")
    tree_type = tree.get("type")
    if tree_type not in BT_TREE_ROOT_TYPES:
        raise BTGenerationError("BT tree root must be sequence or action")
    if tree_type == "sequence" and not isinstance(tree.get("children"), list):
        raise BTGenerationError("sequence tree must contain a children list")
    if tree_type == "action" and not isinstance(tree.get("action"), str):
        raise BTGenerationError("action tree must contain an action string")

    if payload.get("confidence") not in BT_CONFIDENCE_VALUES:
        raise BTGenerationError("confidence must be high, medium, or low")
    if not isinstance(payload.get("screen_type"), str) or not payload.get("screen_type").strip():
        raise BTGenerationError("screen_type must be a non-empty string")

    if payload.get("screen_type") != "UNKNOWN" and not str(payload.get("target_source", "")).strip():
        warnings.append("non-UNKNOWN response with empty target_source")
    if payload.get("screen_type") != "UNKNOWN" and not str(payload.get("why_safe", "")).strip():
        warnings.append("non-UNKNOWN response with empty why_safe")
    return warnings


def _normalize_v7_response_shape(payload: dict) -> dict:
    normalized = dict(payload)
    normalized.setdefault("expected_next", [])
    normalized.setdefault("extract", None)
    normalized.setdefault("target_source", "")
    normalized.setdefault("why_safe", "")
    return normalized


def _call_claude_cli(
    *,
    user_message: str,
    screenshot_path: Path | None,
    timeout_s: float,
    model: str,
    max_budget_usd: float,
    system_prompt_path: Path,
) -> tuple[str, str, dict]:
    content: list[dict[str, object]] = []
    if screenshot_path is not None:
        content.append(
            {
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": "image/png",
                    "data": base64.b64encode(screenshot_path.read_bytes()).decode("ascii"),
                },
            }
        )
    content.append({"type": "text", "text": user_message})
    stream_input = json.dumps(
        {
            "type": "user",
            "message": {
                "role": "user",
                "content": content,
            },
        },
        ensure_ascii=True,
    ) + "\n"

    cmd = [
        "claude",
        "--print",
        "--input-format",
        "stream-json",
        "--output-format",
        "stream-json",
        "--verbose",
        "--permission-mode",
        "bypassPermissions",
        "--model",
        model,
        "--max-budget-usd",
        str(max_budget_usd),
        "--system-prompt-file",
        str(system_prompt_path),
    ]
    try:
        result = subprocess.run(
            cmd,
            input=stream_input,
            capture_output=True,
            text=True,
            timeout=timeout_s,
        )
    except subprocess.TimeoutExpired as exc:
        raise BTGenerationError(f"claude timed out after {timeout_s}s") from exc
    if result.returncode != 0:
        raise BTGenerationError(f"claude exited {result.returncode}: {result.stderr[:500]}")

    events: list[dict] = []
    for line in result.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError as exc:
            raise BTGenerationError(f"claude stream line was not JSON: {exc}") from exc
        if isinstance(event, dict):
            events.append(event)

    outer = next(
        (event for event in reversed(events) if event.get("type") == "result"),
        None,
    )
    if outer is None:
        raise BTGenerationError("claude stream did not produce a result event")
    if outer.get("subtype") == "error" or outer.get("is_error"):
        raise BTGenerationError(f"claude reported error: {(outer.get('result') or '')[:300]}")

    text = outer.get("result")
    if not text:
        raise BTGenerationError("claude returned empty result")
    metadata = {
        "num_turns": outer.get("num_turns", 0),
        "duration_ms": outer.get("duration_ms", 0),
        "total_cost_usd": outer.get("total_cost_usd", 0.0),
        "session_id": outer.get("session_id", ""),
        "model": model,
        "system_prompt_flag": "stream-json",
        "system_prompt_source": "--system-prompt-file",
    }
    return user_message, str(text), metadata


def generate_bt(
    consultation_id: str,
    timeout_s: float = DEFAULT_TIMEOUT_S,
    model: str = DEFAULT_MODEL,
    max_budget_usd: float = DEFAULT_MAX_BUDGET_USD,
) -> dict:
    tree, metadata, prompt_payload, consult_dir = _load_consult_context(consultation_id)
    platform = metadata.get("platform") or prompt_payload.get("platform") or "unknown"
    last_result = prompt_payload.get("last_result")
    load_knowledge(platform)
    load_provisional(platform)
    tier = int(metadata.get("tier") or prompt_payload.get("tier") or 0)
    is_reconsultation = tier > 0
    reconsult_context = ""
    if is_reconsultation:
        failure_reason = str(metadata.get("failure_reason") or (last_result or {}).get("action") or "unknown")
        previous_screen = str((last_result or {}).get("screen") or metadata.get("screen_type_hint") or "unknown")
        reconsult_context = (
            f"\nRECONSULTATION: Previous BT for screen '{previous_screen}' FAILED.\n"
            f"Failure reason: {failure_reason}\n"
            f"DO NOT guess again — research it carefully before proposing the next tree.\n"
        )
        if failure_reason == "wrong_answer_same_question":
            reconsult_context += (
                "WRONG ANSWER DETECTED: The quiz re-presented the same question after submission.\n"
                "The previous BT's answer was incorrect. Fix the question_type or answer selection logic.\n"
            )

    system_prompt = build_consultation_prompt(
        consultation_id=consultation_id,
        platform=platform,
        tree=tree,
        escalation_level="spark_claude",
        spark_attempts=tier,
        reconsult_context=reconsult_context,
        is_reconsultation=is_reconsultation,
    )
    user_message = (
        f"Consultation directory: {consult_dir}\n"
        "Read screenshot.png, tree.json, and metadata.json from that directory.\n"
        "Return exactly one JSON object matching the recipe format. No prose. No markdown fences."
    )
    prompt_logger.info("prompt_codex: system prompt size=%d", len(system_prompt))
    prompt_logger.info("prompt_codex: user message size=%d", len(user_message))
    if len(system_prompt) > 10000:
        prompt_logger.warning("prompt_codex: system prompt size=%d exceeds 10000 chars", len(system_prompt))
    if len(user_message) > 50000:
        prompt_logger.warning("prompt_codex: user message size=%d exceeds 50000 chars", len(user_message))

    sys_prompt_path = consult_dir / "system_prompt.txt"
    sys_prompt_path.write_text(system_prompt, encoding="utf-8")
    (consult_dir / "prompt.txt").write_text(system_prompt, encoding="utf-8")
    (consult_dir / "user_instruction.txt").write_text(user_message, encoding="utf-8")
    screenshot_path = consult_dir / "screenshot.png"
    screenshot_arg = screenshot_path if screenshot_path.exists() else None
    full_user, raw_text, call_metadata = _call_claude_cli(
        user_message=user_message,
        screenshot_path=screenshot_arg,
        timeout_s=timeout_s,
        model=model,
        max_budget_usd=max_budget_usd,
        system_prompt_path=sys_prompt_path,
    )
    (consult_dir / "user_instruction.txt").write_text(full_user, encoding="utf-8")
    (consult_dir / "raw_response.txt").write_text(raw_text, encoding="utf-8")

    candidate = _extract_json_object(raw_text)
    parsed = _normalize_v7_response_shape(json.loads(candidate))
    warnings = _validate_bt(parsed)
    if warnings:
        logger.warning("bt_generator: validation warnings for %s: %s", consultation_id, warnings)

    parsed["_worker_metadata"] = {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "call": call_metadata,
        "warnings": warnings,
    }
    return parsed
