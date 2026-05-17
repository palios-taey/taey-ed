"""Claude CLI BT generation for spark_v2."""

from __future__ import annotations

import json
import logging
import subprocess
import time
from pathlib import Path

from spark_v2.tasks.knowledge_loader import load_knowledge, load_provisional
from spark_v2.tasks.prompt_codex import (
    AXTreeTooLargeError,
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
    system_prompt_path: Path,
) -> tuple[str, dict]:
    # `claude --help` in this environment does not expose an image flag, so the
    # worker uses an explicit Read-tool instruction against the screenshot path.
    full_user = user_message
    if screenshot_path is not None:
        full_user = (
            f"VISION REQUIRED. Before emitting any JSON, you MUST invoke the Read tool exactly once "
            f"on this image path:\n  {screenshot_path}\n\n"
            "The Read tool will return the rendered screenshot as visual content. You need this because "
            "the AX tree alone CANNOT disambiguate visual notation (MathJax fractions vs multiplication, "
            "stacked glyphs, color-coded feedback, modal overlays, button-state changes, etc.). "
            "Skipping the Read step has caused real BT misclassifications in production — "
            "e.g. a vertical-fraction 9/m was misread as 9m because Claude tried to infer "
            "from y-coordinates instead of looking at the rendered image.\n\n"
            "Workflow:\n"
            "  1. Call Read on the path above.\n"
            "  2. Examine what is visually rendered (math notation, colors, button states, modal text).\n"
            "  3. Cross-reference with the AX tree below.\n"
            "  4. Emit ONE JSON BT per the output schema in your system prompt.\n\n"
            "If the Read call fails (image unreadable, file missing), emit screen_type=UNKNOWN "
            "with empty tree and document the read failure in _notes.\n\n"
            f"{user_message}"
        )

    base_cmd = [
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
    ]
    attempts = [
        ("--system-prompt-file", base_cmd + ["--system-prompt-file", str(system_prompt_path)]),
        (
            "--append-system-prompt-file",
            base_cmd + ["--system-prompt", "", "--append-system-prompt-file", str(system_prompt_path)],
        ),
    ]
    last_error: BTGenerationError | None = None
    for flag_path, cmd in attempts:
        try:
            result = subprocess.run(
                cmd,
                input=full_user,
                capture_output=True,
                text=True,
                timeout=timeout_s,
            )
        except subprocess.TimeoutExpired as exc:
            raise BTGenerationError(f"claude timed out after {timeout_s}s") from exc
        if result.returncode != 0:
            stderr = result.stderr[:500]
            if "unknown option" in stderr.lower() or flag_path.lstrip("-") in stderr:
                last_error = BTGenerationError(f"claude rejected {flag_path}: {stderr}")
                continue
            raise BTGenerationError(f"claude exited {result.returncode}: {stderr}")
        try:
            outer = json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            raise BTGenerationError(f"claude stdout was not JSON: {exc}") from exc
        if outer.get("is_error"):
            raise BTGenerationError(
                f"claude reported error: {(outer.get('result') or '')[:300]}"
            )
        # Vision usage check — softened from hard-raise to warning-on-response.
        # claude --print is non-deterministic about tool invocation; hard-failing every
        # time Read is skipped surfaces as user_input_needed on Mac which kills the run.
        # Better: emit the BT but record the omission so we can spot misclassifications
        # downstream and so the auditable-intention output carries the signal.
        vision_used = outer.get("num_turns", 0) >= 2
        vision_warning = None
        if screenshot_path is not None and not vision_used:
            vision_warning = (
                f"claude responded with num_turns={outer.get('num_turns', 0)} — "
                f"Read tool was NOT invoked on the screenshot. BT was generated from AX tree alone. "
                f"Watch for visual-notation misclassifications (math fractions, button states, modal text)."
            )

        text = outer.get("result")
        if not text:
            raise BTGenerationError("claude returned empty result")
        metadata = {
            "num_turns": outer.get("num_turns", 0),
            "duration_ms": outer.get("duration_ms", 0),
            "total_cost_usd": outer.get("total_cost_usd", 0.0),
            "session_id": outer.get("session_id", ""),
            "model": model,
            "system_prompt_flag": flag_path,
            "vision_used": vision_used,
        }
        if vision_warning is not None:
            metadata["vision_warning"] = vision_warning
            logger.warning("bt_generator: %s", vision_warning)
        return text, metadata
    raise last_error or BTGenerationError("claude system prompt file invocation failed")


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
    try:
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
    except AXTreeTooLargeError as exc:
        raise BTGenerationError(str(exc)) from exc

    sys_prompt_path = consult_dir / "system_prompt.txt"
    sys_prompt_path.write_text(system_prompt, encoding="utf-8")
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
        system_prompt_path=sys_prompt_path,
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
