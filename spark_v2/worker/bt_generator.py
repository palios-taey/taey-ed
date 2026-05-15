"""Claude CLI BT generation for spark_v2."""

from __future__ import annotations

import json
import subprocess
import time
from pathlib import Path

from spark_v2.tasks.prompt_codex import compile_prompt

CONSULT_DIR = Path("/tmp/taey-ed-consult-v2")
DEFAULT_MODEL = "claude-opus-4-7"
DEFAULT_TIMEOUT_S = 180.0
DEFAULT_MAX_BUDGET_USD = 2.5


class BTGenerationError(RuntimeError):
    """Raised when BT generation fails."""


def _load_consult_context(consultation_id: str) -> tuple[dict, dict, Path]:
    consult_dir = CONSULT_DIR / consultation_id
    if not consult_dir.exists():
        raise BTGenerationError(f"consultation directory missing: {consult_dir}")
    try:
        tree = json.loads((consult_dir / "tree.json").read_text())
        metadata = json.loads((consult_dir / "metadata.json").read_text())
    except Exception as exc:
        raise BTGenerationError(f"consultation files unreadable: {exc}") from exc
    return tree, metadata, consult_dir


def _build_user_instruction(consultation_id: str, has_failure_log: bool) -> str:
    failure_note = ""
    if has_failure_log:
        failure_note = (
            f"\nIMPORTANT: read /tmp/taey-ed-consult-v2/{consultation_id}/bt_debug.log "
            "before deciding. Use it as failure context.\n"
        )
    return (
        "Generate a behavior tree JSON for the current screen.\n"
        f"The accessibility tree is at /tmp/taey-ed-consult-v2/{consultation_id}/tree.json.\n"
        f"{failure_note}"
        "Return exactly one JSON object with keys: tree, screen_type, expected_next, extract.\n"
        "If no safe action is clear, return screen_type UNKNOWN with an empty sequence tree.\n"
    )


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


def _validate_bt(payload: dict) -> None:
    for key in ("tree", "screen_type", "expected_next", "extract"):
        if key not in payload:
            raise BTGenerationError(f"missing BT response key: {key}")
    tree = payload.get("tree")
    if not isinstance(tree, dict) or "type" not in tree:
        raise BTGenerationError("BT tree must be an object with a type field")


def _call_claude_cli(
    system_prompt: str,
    user_message: str,
    screenshot_path: Path | None,
    timeout_s: float,
    model: str,
    max_budget_usd: float,
) -> tuple[str, dict]:
    full_user = user_message
    if screenshot_path is not None:
        full_user = (
            f"You MUST first use your Read tool to examine this image: {screenshot_path}\n\n"
            "After reading the image, complete the task using both the image and the tree context.\n\n"
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
    tree, metadata, consult_dir = _load_consult_context(consultation_id)
    screen_context = {
        "consultation_id": consultation_id,
        "platform": metadata.get("platform", "unknown"),
        "metadata": metadata,
        "tree": tree,
    }
    system_prompt = compile_prompt(
        metadata.get("platform", "unknown"),
        screen_context,
    )
    user_instruction = _build_user_instruction(
        consultation_id,
        has_failure_log=(consult_dir / "bt_debug.log").exists(),
    )
    (consult_dir / "prompt.txt").write_text(system_prompt, encoding="utf-8")
    (consult_dir / "user_instruction.txt").write_text(user_instruction, encoding="utf-8")
    screenshot_path = consult_dir / "screenshot.png"
    screenshot_arg = screenshot_path if screenshot_path.exists() else None
    text, call_metadata = _call_claude_cli(
        system_prompt=system_prompt,
        user_message=user_instruction,
        screenshot_path=screenshot_arg,
        timeout_s=timeout_s,
        model=model,
        max_budget_usd=max_budget_usd,
    )
    payload = json.loads(_extract_json_object(text))
    _validate_bt(payload)
    payload["_worker_metadata"] = {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "call": call_metadata,
    }
    return payload
