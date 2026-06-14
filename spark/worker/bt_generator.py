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
    create_worker_handoff,
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


def _build_user_instruction(tree_path: str) -> str:
    """The user-facing instruction. The screenshot Read directive is injected
    automatically by call_claude_cli when we pass screenshot_path."""
    return f"""Generate a behavior tree (BT) JSON to advance the screen described in your system prompt.

The accessibility tree for the current screen is at:
  {tree_path}

Use your Read tool on that tree file before answering. Do not attempt to read any
other files or directories.

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


# Composite/control node types that are NOT actions — never rewrite their type.
# Everything else with a `type` is an action shorthand ({type: find_all} etc.).
_STRUCTURAL_NODE_TYPES = {"sequence", "selector", "action", "for_each", "conditional", "fallback"}

# Keys that live at the NODE level and must NEVER be moved into params: node
# identity/label, hoisted store keys, subtree pointers, and the for_each/
# conditional structural keys (items/variable/condition). Everything else on an
# ACTION node is an action parameter and belongs in params:{}.
_NODE_LEVEL_KEYS = {
    "type", "action", "name", "store", "store_to_current",
    "children", "do", "then", "else",
    "items", "variable", "condition", "params",
}


def _normalize_bt_nodes(node) -> None:
    """Hoist node-level keys the Mac BT engine reads from the NODE, not params.

    The Mac engine (app/tasks/bt_core.py:227) reads `node_def.get("store")` —
    node level. Workers sometimes bury `store`/`store_to_current` inside params
    (the per-type recipe format writes them inline, e.g. `find_all: {role:
    AXLink, store: links}`), so node_def.get("store") returns None, the result
    is never stored, and `$links` resolves empty — the whole find_all -> $var
    -> send_to_llm chain silently loses its data. Live RCA 2026-06-12: nav
    `items=$links` arrived EMPTY at the server, the LLM read garbage off the
    screenshot. Hoist deterministically so the BT is correct regardless of where
    the worker wrote the key.
    """
    if isinstance(node, dict):
        # Normalize action-as-node-type -> {type: action, action: X}. The Mac
        # engine runs both `{type: find_all}` and `{type: action, action:
        # find_all}`, but the recipe-conformance collector only counts the
        # `action` FIELD. Live RCA 2026-06-13: the worker emits the action as
        # the node TYPE (e.g. {type: discover_menu, ...}), so conformance saw
        # zero actions and false-rejected every dropdown BT ("missing
        # find_all/discover_menu/select_dropdown_option"). Canonicalize here so
        # validation, conformance, and the Mac all see one shape. Invented
        # actions (e.g. describe_image) still normalize, then fail the
        # not-in-recipe check — the safety net is preserved.
        node_type = node.get("type")
        if (
            isinstance(node_type, str)
            and node_type not in _STRUCTURAL_NODE_TYPES
            and "action" not in node
        ):
            node["action"] = node_type
            node["type"] = "action"
        params = node.get("params")
        if isinstance(params, dict):
            for key in ("store", "store_to_current"):
                if key in params and key not in node:
                    node[key] = params.pop(key)
        # NEST flat action params INTO params:{} — the inverse of the store-hoist.
        # Live RCA 2026-06-14 (operator): the RECURRING 422 was worker VARIANCE,
        # not the YAML. The worker non-deterministically emits action params FLAT
        # at node level ({type:action, action:send_to_llm, question_type:..,
        # question:..}) instead of nested. The Mac reads node['params'] -> {} ->
        # send_to_llm defaults to solve_choice + empty question -> generate_answer
        # !success -> HTTP 422 (never reaches the vision call). Nest deterministically
        # so EVERY build is valid regardless of where the worker put the keys.
        # ACTION nodes only; structural keys (for_each items/variable, conditional
        # condition, subtree pointers, identity) stay at node level.
        if node.get("action"):
            flat = {k: v for k, v in list(node.items()) if k not in _NODE_LEVEL_KEYS}
            if flat:
                p = node.get("params")
                if not isinstance(p, dict):
                    p = {}
                    node["params"] = p
                for k, v in flat.items():
                    if k not in p:          # never override an explicit params value
                        p[k] = v
                    node.pop(k, None)        # remove the flat key from node level
        for child in node.get("children") or []:
            _normalize_bt_nodes(child)
        for nested_key in ("do", "then", "else"):
            _normalize_bt_nodes(node.get(nested_key))
    elif isinstance(node, list):
        for item in node:
            _normalize_bt_nodes(item)


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
    screenshot_path = consult_dir / "screenshot.png"
    if not screenshot_path.exists():
        raise BTGenerationError(
            f"Screenshot missing for {consultation_id}: {screenshot_path}"
        )
    try:
        handoff_dir, prompt_meta = create_worker_handoff(
            tree=tree,
            platform=platform,
            consultation_id=consultation_id,
            screen_type=context["screen_type"],
            screenshot_path=screenshot_path,
            kb_chunks=kb_chunks,
        )
    except ScreenTypeAssemblerError as e:
        raise BTGenerationError(f"Prompt assembly failed for {consultation_id}: {e}") from e
    user_instruction = _build_user_instruction(prompt_meta["tree_path"])
    try:
        (consult_dir / "prompt.txt").write_text(
            Path(prompt_meta["system_prompt_path"]).read_text(encoding="utf-8"),
            encoding="utf-8",
        )
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

    logger.info(
        "bt_generator: assembled prompt for %s (%s chars, artifact=%s, kb_chunks=%s, handoff=%s)",
        consultation_id,
        prompt_meta["prompt_chars"],
        prompt_meta["artifact_path"],
        prompt_meta["kb_chunks_included"],
        prompt_meta["handoff_dir"],
    )

    logger.info(
        f"bt_generator: invoking claude for consult={consultation_id} "
        f"(timeout={timeout_s}s, budget=${max_budget_usd})"
    )
    try:
        raw_text, meta = call_claude_cli(
            system_prompt=Path(prompt_meta["system_prompt_path"]).read_text(encoding="utf-8"),
            user_message=user_instruction,
            screenshot_path=prompt_meta["screenshot_path"],
            timeout_s=timeout_s,
            max_budget_usd=max_budget_usd,
            require_screenshot_read=True,
            permission_mode="dontAsk",
            tools=["Read"],
            add_dirs=[prompt_meta["handoff_dir"]],
            working_dir=prompt_meta["handoff_dir"],
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

    # Correct the store-in-params defect before validation / send (live RCA).
    _normalize_bt_nodes(bt.get("tree", bt))

    _validate_bt(bt, consultation_id)
    try:
        validate_worker_bt_response(bt, platform=platform, screen_type=context["screen_type"])
    except ScreenTypeAssemblerError as e:
        # Observability (2026-06-13): persist the worker's exact BT on a
        # conformance rejection so the failure is diagnosable. The raw output
        # is otherwise lost — we could only infer why the worker's actions
        # weren't recognized. No behavior change; written before the raise.
        try:
            (consult_dir / "rejected_bt.json").write_text(
                json.dumps(bt, indent=2, ensure_ascii=False), encoding="utf-8"
            )
        except Exception:
            logger.exception("failed to persist rejected_bt.json (non-fatal)")
        raise BTGenerationError(
            f"BT recipe-conformance validation failed for {consultation_id}: {e}"
        ) from e

    # Screen session: absorb the worker's _session contribution (facts/plan/
    # lesson) so the NEXT cycle on this screen resumes with what this build
    # measured or decided (Jesse 2026-06-11 per-screen working memory).
    #
    # Guard (2026-06-13 RCA): only absorb from RECIPE-backed builds (artifact
    # kind == yaml). UNKNOWN-guide builds have no recipe and the worker
    # freelances — absorbing those plans poisons the per-screen memory (tonight
    # an UNKNOWN-guide click_at plan got stored, then every later cycle resumed
    # it and rebuilt the rejected click_at BT, ignoring the recipe). See
    # [[poisoned-screen-session-traps-worker]].
    if prompt_meta.get("artifact_kind") == "yaml":
        try:
            from spark.tasks.skeleton import extract_skeleton, skeleton_hash as _sh
            from spark.tasks.screen_session import absorb_worker_session
            absorb_worker_session(platform, _sh(extract_skeleton(tree)), bt)
        except Exception:
            logger.exception("screen_session absorption failed (continuing)")
    else:
        logger.info(
            f"bt_generator: skipped screen_session absorb for {consultation_id} "
            f"(artifact kind={prompt_meta.get('artifact_kind')!r}, not recipe-backed "
            f"— avoids poisoning per-screen memory with freelance plans)"
        )

    logger.info(
        f"bt_generator: success for {consultation_id} in {meta['elapsed_wall_s']:.1f}s "
        f"(turns={meta['num_turns']}, api-equivalent cost "
        f"${meta['total_cost_usd']:.3f}, "
        f"screen_type={bt.get('screen_type')}, root_type={bt['tree'].get('type')})"
    )
    return bt
