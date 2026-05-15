"""Claude CLI BT generation for spark_v2."""

from __future__ import annotations

import json
import logging
import subprocess
import time
from pathlib import Path

from spark_v2.tasks.knowledge_loader import load_knowledge, load_provisional
from spark_v2.tasks.prompt_codex import (
    UNIVERSAL_LAYER_PATH,
    assemble_system_prompt,
    assemble_user_message,
    load_universal_layer_sections,
)

logger = logging.getLogger(__name__)

CONSULT_DIR = Path("/tmp/taey-ed-consult-v2")
DEFAULT_MODEL = "claude-opus-4-7"
DEFAULT_TIMEOUT_S = 180.0
DEFAULT_MAX_BUDGET_USD = 2.5
UNIVERSAL_LAYER_SECTIONS = load_universal_layer_sections(str(UNIVERSAL_LAYER_PATH))


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
    required = (
        "screen_type",
        "tree",
        "expected_next",
        "extract",
        "target_source",
        "why_safe",
        "confidence",
    )
    for key in required:
        if key not in payload:
            raise BTGenerationError(f"missing BT response key: {key}")

    tree = payload.get("tree")
    if not isinstance(tree, dict):
        raise BTGenerationError("BT tree must be a JSON object")
    tree_type = tree.get("type")
    if tree_type not in {"sequence", "action"}:
        raise BTGenerationError("BT tree root must be sequence or action")
    if tree_type == "sequence" and not isinstance(tree.get("children"), list):
        raise BTGenerationError("sequence tree must contain a children list")
    if tree_type == "action" and not isinstance(tree.get("action"), str):
        raise BTGenerationError("action tree must contain an action string")

    if payload.get("confidence") not in {"high", "medium", "low"}:
        raise BTGenerationError("confidence must be high, medium, or low")
    if not isinstance(payload.get("screen_type"), str) or not payload.get("screen_type").strip():
        raise BTGenerationError("screen_type must be a non-empty string")

    if payload.get("screen_type") != "UNKNOWN" and not str(payload.get("target_source", "")).strip():
        warnings.append("non-UNKNOWN response with empty target_source")
    if payload.get("screen_type") != "UNKNOWN" and not str(payload.get("why_safe", "")).strip():
        warnings.append("non-UNKNOWN response with empty why_safe")
    return warnings


def _call_claude_cli(
    *,
    system_prompt: str,
    user_message: str,
    screenshot_path: Path | None,
    timeout_s: float,
    model: str,
    max_budget_usd: float,
) -> tuple[str, dict]:
    # `claude --help` in this environment does not expose an image flag, so the
    # worker uses an explicit Read-tool instruction against the screenshot path.
    full_user = user_message
    if screenshot_path is not None:
        full_user = (
            f"You MUST first use your Read tool to examine this image: {screenshot_path}\n\n"
            "After reading the image, complete the task using both the image and the AX tree context.\n\n"
            f"{user_message}"
        )

    cmd = [
        "claude",
        "--print",
        "--output-format",
        "json",
        "--permission-mode",
        "bypassPermissions",
        "--model",
        model,
        "--max-budget-usd",
        str(max_budget_usd),
        "--system-prompt",
        system_prompt,
        full_user,
    ]
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout_s,
            stdin=subprocess.DEVNULL,
        )
    except subprocess.TimeoutExpired as exc:
        raise BTGenerationError(f"claude timed out after {timeout_s}s") from exc
    if result.returncode != 0:
        raise BTGenerationError(
            f"claude exited {result.returncode}: {result.stderr[:500]}"
        )
    try:
        outer = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise BTGenerationError(f"claude stdout was not JSON: {exc}") from exc
    if outer.get("is_error"):
        raise BTGenerationError(
            f"claude reported error: {(outer.get('result') or '')[:300]}"
        )
    if screenshot_path is not None and outer.get("num_turns", 0) < 2:
        raise BTGenerationError("claude did not read the screenshot before answering")

    text = outer.get("result")
    if not text:
        raise BTGenerationError("claude returned empty result")
    metadata = {
        "num_turns": outer.get("num_turns", 0),
        "duration_ms": outer.get("duration_ms", 0),
        "total_cost_usd": outer.get("total_cost_usd", 0.0),
        "session_id": outer.get("session_id", ""),
        "model": model,
    }
    return text, metadata


def generate_bt(
    consultation_id: str,
    timeout_s: float = DEFAULT_TIMEOUT_S,
    model: str = DEFAULT_MODEL,
    max_budget_usd: float = DEFAULT_MAX_BUDGET_USD,
) -> dict:
    tree, metadata, prompt_payload, consult_dir = _load_consult_context(consultation_id)
    platform = metadata.get("platform") or prompt_payload.get("platform") or "unknown"
    platform_data = load_knowledge(platform)
    provisional_data = load_provisional(platform)
    last_result = prompt_payload.get("last_result")
    tier = int(metadata.get("tier") or prompt_payload.get("tier") or 0)

    system_prompt = assemble_system_prompt(
        universal_sections=UNIVERSAL_LAYER_SECTIONS,
        platform_data=platform_data,
        provisional_data=provisional_data,
        last_result=last_result,
        tier=tier,
    )
    user_message = assemble_user_message(
        platform_display_name=platform_data.get("platform", {}).get("display_name", platform),
        current_url=prompt_payload.get("current_url"),
        last_screen_type=(last_result or {}).get("screen"),
        tier=tier,
        course_id=((prompt_payload.get("client_state") or {}).get("course_id")),
        tree=tree,
        screenshot_path=str(consult_dir / "screenshot.png"),
        relevant_kb_chunks=prompt_payload.get("relevant_kb_chunks"),
        screen_context={
            "tree": tree,
            "screen_type_hint": metadata.get("screen_type_hint") or (last_result or {}).get("screen"),
        },
    )

    (consult_dir / "prompt.txt").write_text(system_prompt, encoding="utf-8")
    (consult_dir / "user_instruction.txt").write_text(user_message, encoding="utf-8")

    screenshot_path = consult_dir / "screenshot.png"
    screenshot_arg = screenshot_path if screenshot_path.exists() else None
    raw_text, call_metadata = _call_claude_cli(
        system_prompt=system_prompt,
        user_message=user_message,
        screenshot_path=screenshot_arg,
        timeout_s=timeout_s,
        model=model,
        max_budget_usd=max_budget_usd,
    )
    (consult_dir / "raw_response.txt").write_text(raw_text, encoding="utf-8")

    candidate = _extract_json_object(raw_text)
    parsed = json.loads(candidate)
    warnings = _validate_bt(parsed)
    if warnings:
        logger.warning("bt_generator: validation warnings for %s: %s", consultation_id, warnings)

    parsed["_worker_metadata"] = {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "call": call_metadata,
        "warnings": warnings,
    }
    return parsed
