"""Claude-CLI invocation that converts a consultation into a behavior tree.

The Mac-side BT engine sends a screen to Spark; if Spark needs LLM help on a
fresh / unfamiliar screen, this module is what asks Claude (via the CLI over
Jesse's Max subscription) to produce a BT. Pure single-shot: same screen +
tree + screenshot + compiled instructions go in, BT JSON comes out.

The subprocess + JSON-wrapper plumbing lives in spark.tasks.claude_runner;
this module focuses on assembling the prompt and validating the BT shape.
"""

import json
import logging
from pathlib import Path

from spark.tasks.claude_runner import (
    call_claude_cli,
    ClaudeCallError,
    DEFAULT_MAX_BUDGET_USD,
)
from spark.tasks.screen_type_assembler import (
    ScreenTypeAssemblerError,
    assemble_worker_prompt,
    validate_worker_bt_response,
)

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT_S = 180


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
    """The user-facing instruction. The screenshot Read directive is injected
    automatically by call_claude_cli when we pass screenshot_path."""
    failure_note = (
        f"\nIMPORTANT: a bt_debug.log exists in /tmp/taey-ed-consult/{consultation_id}/. "
        f"Read it FIRST — it tells you why the prior BT failed. Adjust accordingly.\n"
        if has_failure_log else ""
    )
    return f"""Generate a behavior tree (BT) JSON to advance the screen described in your system prompt.

The accessibility tree for the current screen is at:
  /tmp/taey-ed-consult/{consultation_id}/tree.json
{failure_note}
Shape:
{{
  "tree": <BT root node>,
  "screen_type": "<canonical screen_type from the selected YAML, or UNKNOWN-guide classification>",
  "expected_next": ["<screen_type>", ...],
  "extract": null,
  "_session": {{
    "facts": {{}},
    "plan": null,
    "lesson": ""
  }}
}}

Only emit "_session" if you learned something the next cycle must retain for THIS screen.

Never click "Skip" mid-exercise; "Up next" only on post-completion transitions.

==============================================================
OUTPUT FORMAT — READ THIS LAST, FOLLOW THIS FIRST
==============================================================
Your ENTIRE response must be a single JSON object. Nothing else.

- NO prose preamble. NO "Based on the screenshot...". NO "Looking at the tree...".
- NO analysis text before the JSON. NO commentary after.
- NO markdown. NO code fences. NO ```json wrapper.
- The FIRST CHARACTER of your response must be an opening brace.
- The LAST CHARACTER of your response must be a closing brace.

Reason silently. Emit JSON. That is the whole response.
=============================================================="""


def _extract_json_object(text: str) -> str:
    """Extract the first balanced JSON object from arbitrary text, tolerating
    prose preamble / trailing commentary / markdown code fences. Returns the
    JSON substring ready for json.loads().

    Strict instructions in the prompt should prevent Claude from emitting
    anything besides the JSON object, but long system prompts sometimes lose
    that battle ("Looking at the screenshot..." leaks through). This is the
    parser-side safety net so a single preamble token doesn't cost a full
    consultation + fallback cycle.

    Raises ValueError if no balanced object is found.
    """
    text = text.strip()
    # Strip markdown code fences if present
    if text.startswith("```"):
        lines = text.split("\n")
        if lines and lines[-1].strip() == "```":
            lines = lines[1:-1]
        else:
            lines = lines[1:]
        text = "\n".join(lines).strip()

    start = text.find("{")
    if start < 0:
        raise ValueError("no JSON object found in response (no opening brace)")

    depth = 0
    in_string = False
    escape = False
    for i in range(start, len(text)):
        ch = text[i]
        if escape:
            escape = False
            continue
        if in_string:
            if ch == "\\":
                escape = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return text[start:i+1]

    raise ValueError(
        f"unbalanced JSON object in response (depth={depth} at end of text)"
    )


# Kept as a back-compat alias; new code should call _extract_json_object.
def _strip_code_fences(text: str) -> str:
    return _extract_json_object(text)


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
    if "_session" in parsed and not isinstance(parsed["_session"], dict):
        raise BTGenerationError(
            f"BT response for {consultation_id}: '_session' must be a dict when present"
        )


def generate_bt(
    consultation_id: str,
    timeout_s: float = DEFAULT_TIMEOUT_S,
    max_budget_usd: float = DEFAULT_MAX_BUDGET_USD,
) -> dict:
    """Invoke Claude CLI to generate a BT for the given consultation.

    Returns the parsed response.json dict (tree, screen_type, expected_next, extract).
    Raises BTGenerationError on any failure (timeout, non-zero exit, invalid JSON,
    schema mismatch, blind answer with no screenshot read).

    The caller is responsible for:
      - Ensuring the consultation directory exists with tree.json, screenshot.png,
        and metadata.json
      - Writing the returned dict to response.json + marking metadata complete
    """
    consult_dir = Path(f"/tmp/taey-ed-consult/{consultation_id}")
    if not consult_dir.exists():
        raise BTGenerationError(f"Consultation directory missing: {consult_dir}")

    tree, metadata = _load_consult_context(consultation_id)
    platform = metadata.get("platform", "khan_academy")

    context = {
        "escalation_level": metadata.get("escalation_level", "spark_claude"),
        "course_id": metadata.get("course_id", "unknown"),
        "failure_reason": metadata.get("failure_reason", ""),
        "previous_screen": metadata.get("previous_screen_type", ""),
        "screen_type": metadata.get("screen_type_hint", "UNKNOWN"),
    }
    kb_chunks = metadata.get("relevant_kb_chunks") or []
    try:
        system_prompt, prompt_meta = assemble_worker_prompt(
            tree=tree,
            platform=platform,
            consultation_id=consultation_id,
            screen_type=context["screen_type"],
            kb_chunks=kb_chunks,
        )
    except ScreenTypeAssemblerError as e:
        raise BTGenerationError(f"Prompt assembly failed for {consultation_id}: {e}") from e
    logger.info(
        "bt_generator: assembled prompt for %s (%s chars, artifact=%s, kb_chunks=%s)",
        consultation_id,
        prompt_meta["prompt_chars"],
        prompt_meta["artifact_path"],
        prompt_meta["kb_chunks_included"],
    )

    has_failure_log = (consult_dir / "bt_debug.log").exists()
    user_instruction = _build_user_instruction(consultation_id, has_failure_log)
    try:
        (consult_dir / "prompt.txt").write_text(system_prompt, encoding="utf-8")
        (consult_dir / "user_instruction.txt").write_text(
            user_instruction,
            encoding="utf-8",
        )
    except OSError as e:
        logger.warning(
            "bt_generator: failed to write prompt artifacts for %s: %s",
            consultation_id,
            e,
        )

    screenshot_path = consult_dir / "screenshot.png"
    if not screenshot_path.exists():
        raise BTGenerationError(
            f"Screenshot missing for {consultation_id}: {screenshot_path}"
        )

    logger.info(
        f"bt_generator: invoking claude for consult={consultation_id} "
        f"(timeout={timeout_s}s, budget=${max_budget_usd})"
    )
    try:
        raw_text, meta = call_claude_cli(
            system_prompt=system_prompt,
            user_message=user_instruction,
            screenshot_path=str(screenshot_path),
            timeout_s=timeout_s,
            max_budget_usd=max_budget_usd,
            require_screenshot_read=True,
        )
    except ClaudeCallError as e:
        raise BTGenerationError(
            f"Claude call failed for {consultation_id}: {e}"
        ) from e

    try:
        inner_text = _extract_json_object(raw_text)
    except ValueError as e:
        raise BTGenerationError(
            f"BT JSON for {consultation_id} extraction failed: {e}; "
            f"head: {raw_text[:300]}"
        ) from e
    try:
        bt = json.loads(inner_text)
    except json.JSONDecodeError as e:
        raise BTGenerationError(
            f"BT JSON for {consultation_id} parse failed: {e}; head: {inner_text[:300]}"
        ) from e

    _validate_bt(bt, consultation_id)
    try:
        validate_worker_bt_response(bt, platform=platform, screen_type=context["screen_type"])
    except ScreenTypeAssemblerError as e:
        raise BTGenerationError(
            f"BT recipe-conformance validation failed for {consultation_id}: {e}"
        ) from e

    # Screen session: absorb the worker's _session contribution (facts/plan/
    # lesson) so the NEXT cycle on this screen resumes with what this build
    # measured or decided (Jesse 2026-06-11 per-screen working memory).
    try:
        from spark.tasks.skeleton import extract_skeleton, skeleton_hash as _sh
        from spark.tasks.screen_session import absorb_worker_session
        absorb_worker_session(platform, _sh(extract_skeleton(tree)), bt)
    except Exception:
        logger.exception("screen_session absorption failed (continuing)")

    logger.info(
        f"bt_generator: success for {consultation_id} in {meta['elapsed_wall_s']:.1f}s "
        f"(turns={meta['num_turns']}, api-equivalent cost "
        f"${meta['total_cost_usd']:.3f}, "
        f"screen_type={bt.get('screen_type')}, root_type={bt['tree'].get('type')})"
    )
    return bt
