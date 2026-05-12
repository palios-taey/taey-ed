"""Headless Claude CLI invocation that converts a consultation into a BT.

The Mac-side BT engine sends a screen to Spark; if Spark needs LLM-shaped
help (UNKNOWN screen / failure context), this module is the one that asks
Claude (headless, via the CLI installed on Mira) for the BT.

Proven 2026-05-11: `claude --print --output-format json --permission-mode
bypassPermissions` works against the Max subscription on Mira. The wrapper
JSON has `result` field containing the model's text output (which we then
parse as the BT JSON).

Per LAUNCH_PLAN.md Phase 2 — replaces the tmux-notify-interactive-Claude
path with autonomous worker-callable BT generation.
"""

import json
import logging
import subprocess
import time
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

CLAUDE_BIN = "claude"
DEFAULT_TIMEOUT_S = 120
DEFAULT_MAX_BUDGET_USD = 0.50  # cap per invocation; subscription mode bills $0


class BTGenerationError(RuntimeError):
    """Raised on any failure during BT generation: invocation, parsing,
    schema validation."""


def _load_consult_context(consultation_id: str) -> tuple[dict, dict]:
    """Load tree + metadata from the consult dir."""
    consult_dir = Path(f"/tmp/taey-ed-consult/{consultation_id}")
    if not consult_dir.exists():
        raise BTGenerationError(f"Consultation directory missing: {consult_dir}")
    try:
        tree = json.loads((consult_dir / "tree.json").read_text())
        metadata = json.loads((consult_dir / "metadata.json").read_text())
    except (FileNotFoundError, json.JSONDecodeError) as e:
        raise BTGenerationError(
            f"Consultation {consultation_id} files missing or malformed: {e}"
        ) from e
    return tree, metadata


def _build_user_instruction(consultation_id: str, has_failure_log: bool) -> str:
    """The user-facing instruction we send to Claude. Brief because the
    full platform knowledge + handler reference + operational notes are
    already in the system prompt (from compile_prompt)."""
    failure_note = (
        f"\nIMPORTANT: a bt_debug.log exists in /tmp/taey-ed-consult/{consultation_id}/. "
        f"Read it FIRST — it tells you why the prior BT failed. Adjust accordingly.\n"
        if has_failure_log else ""
    )
    return f"""Generate a behavior tree (BT) JSON to advance the screen described in your system prompt.

Use the Read tool to examine:
  /tmp/taey-ed-consult/{consultation_id}/screenshot.png  (the visual)
  /tmp/taey-ed-consult/{consultation_id}/tree.json       (full AX tree)
{failure_note}
Output ONLY the response.json content — no commentary, no markdown fences.

Shape:
{{
  "tree": <BT root node>,
  "screen_type": "<NAVIGATION | VIDEO | ARTICLE | EXERCISE | TRANSITION | UNKNOWN>",
  "expected_next": ["<screen_type>", ...],
  "extract": null
}}

Never click "Skip" mid-exercise; "Up next" only on post-completion transitions."""


def _strip_code_fences(text: str) -> str:
    """Strip leading/trailing ```json or ``` if Claude wrapped output."""
    text = text.strip()
    if text.startswith("```"):
        lines = text.split("\n")
        if lines[-1].strip() == "```":
            lines = lines[1:-1]
        else:
            lines = lines[1:]
        text = "\n".join(lines).strip()
    return text


def _validate_bt(parsed: dict, consultation_id: str) -> None:
    """Validate the parsed BT response has the required shape.

    Minimal schema check — the BT engine on the Mac is robust to many shapes
    but the top-level keys must be present.
    """
    required = ("tree", "screen_type", "expected_next", "extract")
    for k in required:
        if k not in parsed:
            raise BTGenerationError(
                f"BT response for {consultation_id} missing required key: {k!r}"
            )
    if not isinstance(parsed["tree"], dict):
        raise BTGenerationError(
            f"BT response for {consultation_id}: 'tree' must be a dict, "
            f"got {type(parsed['tree']).__name__}"
        )
    if "type" not in parsed["tree"]:
        raise BTGenerationError(
            f"BT response for {consultation_id}: tree missing 'type' field"
        )


def generate_bt(
    consultation_id: str,
    timeout_s: float = DEFAULT_TIMEOUT_S,
    max_budget_usd: float = DEFAULT_MAX_BUDGET_USD,
) -> dict:
    """Invoke Claude CLI headlessly to generate a BT for the given consultation.

    Returns the parsed response.json dict (tree, screen_type, expected_next, extract).
    Raises BTGenerationError on any failure (timeout, non-zero exit, invalid JSON,
    schema mismatch).

    The caller is responsible for:
      - Ensuring the consultation directory exists with tree.json, screenshot.png,
        and metadata.json
      - Writing the returned dict to response.json + marking metadata complete

    Args:
        consultation_id: The consultation UUID matching /tmp/taey-ed-consult/{id}/
        timeout_s: Wall-clock timeout for the claude invocation
        max_budget_usd: API-equivalent spend cap (informational on subscription mode)
    """
    consult_dir = Path(f"/tmp/taey-ed-consult/{consultation_id}")
    if not consult_dir.exists():
        raise BTGenerationError(f"Consultation directory missing: {consult_dir}")

    tree, metadata = _load_consult_context(consultation_id)
    platform = metadata.get("platform", "khan_academy")

    # Build the comprehensive system prompt with platform knowledge, handler
    # reference, operational notes (exactly what the tmux-Claude path used).
    from spark.tasks.prompt_codex import compile_prompt
    context = {
        "escalation_level": metadata.get("escalation_level", "spark_claude"),
        "course_id": metadata.get("course_id", "unknown"),
        "failure_reason": metadata.get("failure_reason", ""),
        "previous_screen": metadata.get("previous_screen_type", ""),
        "screen_type": metadata.get("screen_type_hint", "UNKNOWN"),
    }
    system_prompt = compile_prompt(
        tree=tree,
        platform=platform,
        consultation_id=consultation_id,
        context=context,
        spark_attempts=metadata.get("spark_attempts", 0),
        is_reconsultation=bool(metadata.get("failure_reason")),
    )

    has_failure_log = (consult_dir / "bt_debug.log").exists()
    user_instruction = _build_user_instruction(consultation_id, has_failure_log)

    cmd = [
        CLAUDE_BIN,
        "--print",
        "--output-format", "json",
        "--permission-mode", "bypassPermissions",
        "--max-budget-usd", str(max_budget_usd),
        "--system-prompt", system_prompt,
        user_instruction,
    ]

    t0 = time.time()
    logger.info(
        f"bt_generator: invoking claude --print for consult={consultation_id} "
        f"(timeout={timeout_s}s, budget=${max_budget_usd})"
    )
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout_s,
            stdin=subprocess.DEVNULL,
        )
    except subprocess.TimeoutExpired as e:
        raise BTGenerationError(
            f"Claude invocation timed out after {timeout_s}s for {consultation_id}"
        ) from e

    elapsed = time.time() - t0

    if result.returncode != 0:
        raise BTGenerationError(
            f"Claude exit code {result.returncode} for {consultation_id}; "
            f"stderr: {result.stderr[:500]}"
        )

    # Parse outer wrapper (Claude CLI's JSON output format)
    try:
        outer = json.loads(result.stdout)
    except json.JSONDecodeError as e:
        raise BTGenerationError(
            f"Claude stdout for {consultation_id} is not JSON: {e}; "
            f"head: {result.stdout[:300]}"
        ) from e

    if outer.get("is_error"):
        raise BTGenerationError(
            f"Claude reported error for {consultation_id}: "
            f"subtype={outer.get('subtype')} result={outer.get('result', '')[:300]}"
        )

    inner_text = outer.get("result", "")
    if not inner_text:
        raise BTGenerationError(
            f"Claude returned empty result for {consultation_id}"
        )

    # Strip code fences if present, then parse as the BT response dict
    inner_text = _strip_code_fences(inner_text)
    try:
        bt = json.loads(inner_text)
    except json.JSONDecodeError as e:
        raise BTGenerationError(
            f"BT JSON for {consultation_id} parse failed: {e}; "
            f"head: {inner_text[:300]}"
        ) from e

    _validate_bt(bt, consultation_id)

    cost_info = outer.get("total_cost_usd", 0)
    logger.info(
        f"bt_generator: success for {consultation_id} in {elapsed:.1f}s "
        f"(api-equivalent cost ${cost_info:.3f}, "
        f"screen_type={bt.get('screen_type')}, "
        f"root_type={bt['tree'].get('type')})"
    )
    return bt
