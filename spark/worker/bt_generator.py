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

from jsonschema import Draft202012Validator

from spark.tasks.claude_runner import (
    call_claude_cli,
    ClaudeCallError,
    DEFAULT_MAX_BUDGET_USD,
)
from spark.tasks.screen_type_assembler import (
    KNOWN_ACTIONS,
    ScreenTypeAssemblerError,
    _collect_tree_actions,
    create_worker_handoff,
    validate_worker_bt_response,
)

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT_S = 180
WORKER_SCHEMA_MAX_ATTEMPTS = 2


class BTGenerationError(RuntimeError):
    """Raised on any failure during BT generation: invocation, parsing,
    schema validation."""

    def __init__(
        self,
        message: str,
        *,
        failure_kind: str = "worker_pipeline",
        rejected_bt_path: str | None = None,
        worker_raw_response_path: str | None = None,
        worker_raw_stdout_path: str | None = None,
        worker_output=None,
    ):
        super().__init__(message)
        self.failure_kind = failure_kind
        self.rejected_bt_path = rejected_bt_path
        self.worker_raw_response_path = worker_raw_response_path
        self.worker_raw_stdout_path = worker_raw_stdout_path
        self.worker_output = worker_output


WORKER_RAW_STDOUT_NAME = "worker_raw_stdout.txt"
WORKER_RAW_RESPONSE_NAME = "worker_raw_response.json"
WORKER_ESCALATE_NAME = "worker_escalate.json"
WORKER_ESCALATE_KIND = "worker_escalate"

WORKER_BT_OUTPUT_SCHEMA = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "required": [
        "tree",
        "screen_type",
        "expected_next",
        "extract",
        "slots",
        "evidence",
        "confidence",
    ],
    "properties": {
        "tree": {"type": "object"},
        "screen_type": {"type": "string", "minLength": 1},
        "expected_next": {
            "type": "array",
            "items": {"type": "string"},
        },
        "extract": {},
        "slots": {
            "type": "object",
            "additionalProperties": True,
        },
        "evidence": {
            "type": "object",
            "required": ["target_source", "why_safe", "never_clicks_avoided"],
            "properties": {
                "target_source": {"type": "string", "minLength": 1},
                "why_safe": {"type": "string", "minLength": 1},
                "never_clicks_avoided": {
                    "oneOf": [
                        {"type": "string", "minLength": 1},
                        {
                            "type": "array",
                            "minItems": 1,
                            "items": {"type": "string", "minLength": 1},
                        },
                    ],
                },
            },
            "additionalProperties": True,
        },
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
        "_session": {"type": "object"},
    },
    "not": {"required": ["escalate"]},
    "additionalProperties": True,
}
WORKER_ESCALATE_OUTPUT_SCHEMA = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "required": ["screen_type", "escalate", "confidence"],
    "properties": {
        "screen_type": {"type": "string", "minLength": 1},
        "escalate": {
            "type": "object",
            "required": ["reason", "evidence", "never_clicks"],
            "properties": {
                "reason": {"type": "string", "minLength": 1},
                "evidence": {
                    "oneOf": [
                        {"type": "string", "minLength": 1},
                        {"type": "object", "minProperties": 1},
                    ],
                },
                "never_clicks": {
                    "type": "array",
                    "minItems": 1,
                    "items": {"type": "string", "minLength": 1},
                },
            },
            "additionalProperties": True,
        },
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
        "_session": {"type": "object"},
    },
    "not": {
        "anyOf": [
            {"required": ["tree"]},
            {"required": ["slots"]},
        ]
    },
    "additionalProperties": True,
}
WORKER_OUTPUT_SCHEMA = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "oneOf": [WORKER_BT_OUTPUT_SCHEMA, WORKER_ESCALATE_OUTPUT_SCHEMA],
}
WORKER_OUTPUT_SCHEMA_VALIDATOR = Draft202012Validator(WORKER_OUTPUT_SCHEMA)


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
  "slots": {{
    "<slot_name>": "<slot value copied or derived for this screen>"
  }},
  "evidence": {{
    "target_source": "<exact text/name/value from the tree or screenshot that anchors the chosen target>",
    "why_safe": "<why this BT advances the current screen without clicking forbidden controls>",
    "never_clicks_avoided": ["Skip", "Take again", "mastery marketing controls"]
  }},
  "confidence": 0.0,
  "_session": {{
    "facts": {{}},
    "plan": null,
    "lesson": ""
  }}
}}

If the screen must not be automated safely, emit this escalation envelope instead
of a BT. Do not emit "tree": {{}} as a refusal:
{{
  "screen_type": "<canonical screen_type>",
  "escalate": {{
    "reason": "<why automation must stop and the ladder must handle recovery>",
    "evidence": {{
      "target_source": "<exact tree/screenshot signal>",
      "why_safe": "<why no click/submit/retry is safe here>"
    }},
    "never_clicks": ["Try again", "Skip", "answer widget", "Check"]
  }},
  "confidence": 0.0
}}

Only emit "_session" if you learned something the next cycle must retain for THIS screen.
Use "slots": {{}} only when there is no typed slot value for this screen yet.
The server validates screen_type, slots, evidence, and confidence with JSON Schema
before storage or execution; missing fields or wrong types are rejected. A valid
escalation envelope is routed directly to the ladder without a schema retry.
confidence is a number from 0.0 to 1.0.

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


def _persist_text_artifact(path: Path, text: str) -> str | None:
    try:
        path.write_text(text, encoding="utf-8")
        return str(path)
    except Exception:
        logger.exception("failed to persist %s", path.name)
        return None


def _persist_json_artifact(path: Path, payload) -> str | None:
    try:
        path.write_text(
            json.dumps(payload, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        return str(path)
    except Exception:
        logger.exception("failed to persist %s", path.name)
        return None


def _schema_error_path(error) -> str:
    path = "$"
    for part in error.absolute_path:
        if isinstance(part, int):
            path += f"[{part}]"
        else:
            path += f".{part}"
    return path


def _schema_error_summary(errors) -> str:
    ordered = sorted(errors, key=lambda e: list(e.absolute_path))
    return "; ".join(
        f"{_schema_error_path(error)}: {error.message}"
        for error in ordered[:6]
    )


def _build_retry_instruction(user_instruction: str, rejection: str) -> str:
    return f"""{user_instruction}

==============================================================
SERVER-SIDE WORKER OUTPUT REJECTION
==============================================================
Your previous response was rejected before storage or execution:
{rejection}

Emit a complete corrected JSON object now. Do not explain the rejection.
Keep the same task and include the required top-level worker contract fields:
screen_type, slots, evidence, and confidence. If automation must refuse safely,
emit the explicit escalation envelope instead of a BT.
=============================================================="""


def _persist_rejection_artifact(path: Path, error: BTGenerationError) -> str | None:
    payload = {
        "failure_kind": error.failure_kind,
        "reason": str(error),
        "worker_raw_response_path": error.worker_raw_response_path,
        "worker_raw_stdout_path": error.worker_raw_stdout_path,
        "worker_output": error.worker_output,
    }
    return _persist_json_artifact(path, payload)


def _is_worker_escalate_output(parsed: dict) -> bool:
    return isinstance(parsed, dict) and isinstance(parsed.get("escalate"), dict)


def _canonical_worker_output(parsed):
    if not isinstance(parsed, dict):
        return parsed
    if _is_worker_escalate_output(parsed):
        return parsed
    evidence = parsed.get("evidence")
    if parsed.get("tree") == {} and isinstance(evidence, dict):
        never_clicks = evidence.get("never_clicks_avoided")
        if isinstance(never_clicks, str):
            never_clicks = [never_clicks]
        if not isinstance(never_clicks, list) or not never_clicks:
            never_clicks = ["unsafe automation"]
        reason = str(
            evidence.get("why_safe")
            or evidence.get("target_source")
            or "worker refused to emit an executable behavior tree"
        ).strip()
        return {
            "screen_type": str(parsed.get("screen_type") or "UNKNOWN").strip() or "UNKNOWN",
            "escalate": {
                "reason": reason,
                "evidence": evidence,
                "never_clicks": [str(item) for item in never_clicks if str(item).strip()],
                "source": "legacy_empty_tree_refusal",
            },
            "confidence": parsed.get("confidence", 0.0),
        }
    return parsed


def _worker_escalate_error(
    parsed: dict,
    consultation_id: str,
    *,
    worker_raw_response_path: str | None = None,
    worker_raw_stdout_path: str | None = None,
) -> BTGenerationError:
    envelope = parsed.get("escalate") or {}
    never_clicks = envelope.get("never_clicks") or []
    if isinstance(never_clicks, list):
        never_clicks_text = ", ".join(str(item) for item in never_clicks)
    else:
        never_clicks_text = str(never_clicks)
    reason = str(envelope.get("reason") or "worker requested escalation").strip()
    return BTGenerationError(
        f"Worker escalation envelope for {consultation_id}: {reason}; "
        f"never_clicks={never_clicks_text}",
        failure_kind=WORKER_ESCALATE_KIND,
        worker_raw_response_path=worker_raw_response_path,
        worker_raw_stdout_path=worker_raw_stdout_path,
        worker_output=parsed,
    )


def _root_shape_summary(value) -> str:
    if isinstance(value, dict):
        keys = sorted(str(k) for k in value.keys())
        preview = json.dumps(value, ensure_ascii=False, default=str)[:500]
        return f"dict keys={keys} preview={preview}"
    if isinstance(value, list):
        return f"list len={len(value)}"
    return type(value).__name__


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
_ACTION_AS_KEY_METADATA_KEYS = {"comment"}


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
        # COMPOSITE-AS-KEY form: workers can emit a structural node as the sole
        # dict key (live 2026-07-10 dropdown: else: {sequence: [...]}); without a
        # type/children rewrite the Mac sees an untyped dict and never recurses.
        if "type" not in node and "action" not in node:
            _ckeys = [k for k in node.keys() if k in {"sequence", "fallback"}]
            if len(_ckeys) == 1 and len(node) == 1:
                _ck = _ckeys[0]
                _val = node.pop(_ck)
                if isinstance(_val, list):
                    node["type"] = _ck
                    node["children"] = _val
        # ACTION-AS-KEY form FIRST: the worker sometimes emits {<action>: {<params>}}
        # — the action name as the dict KEY — instead of {type:action,
        # action:<action>, params:{...}}. Neither _collect_tree_actions nor the Mac
        # recognizes it, so every action is reported omitted -> conformance
        # rejects a perfectly good BT. (RCA 2026-06-15, d2b842: the worker emitted
        # {"find_and_click": {...}}, {"wait": 1.5}, {"store_qa": {...}}.) Canonicalize
        # action. The only tolerated sibling is non-execution metadata such as
        # `comment`, which is carried into the node label. Runs BEFORE the
        # for_each/conditional canonicalization so {"for_each": {...}} flows
        # through both.
        if "type" not in node and "action" not in node:
            _akeys = [k for k in node.keys() if k in KNOWN_ACTIONS]
            _non_action_keys = [k for k in node.keys() if k not in _akeys]
            if (
                len(_akeys) == 1
                and all(k in _ACTION_AS_KEY_METADATA_KEYS for k in _non_action_keys)
            ):
                _ak = _akeys[0]
                _val = node.pop(_ak)
                _comment = node.pop("comment", None)
                node["type"] = "action"
                node["action"] = _ak
                if isinstance(_comment, str) and _comment:
                    node["name"] = _comment
                if isinstance(_val, dict):
                    node["params"] = _val
                elif _ak == "wait" and isinstance(_val, (int, float)):
                    node["params"] = {"seconds": _val}
                else:
                    node["params"] = {}
        # ACTION-IN-NAME-FIELD form: the worker sometimes puts the action in the
        # `name` field instead of `action` — {type:action, name:"find_and_click",
        # target:.., role:..} (flat params). _collect_tree_actions reads `action`
        # (None here) so it reports the action OMITTED and conformance rejects an
        # otherwise-correct BT. (RCA 2026-06-15: 2f83dfe4 multi-select built a
        # CORRECT direct-solve B+D but as name-field nodes; same shape as cef8155e's
        # freelance.) Treat `name` as the action ONLY when there is no `action`
        # field AND name is EXACTLY a registered action (else name is a legitimate
        # label). The flat params then nest via the action-param block below.
        if "action" not in node and node.get("name") in KNOWN_ACTIONS:
            node["action"] = node.pop("name")
            node.setdefault("type", "action")
        # ACTION-IN-TOOL-FIELD form: the worker sometimes names the action in a
        # `tool` field instead of `action` — {type:action, name:"click_choice_B",
        # tool:"find_and_click", params:{...}}. _collect_tree_actions and the Mac
        # engine read `action` (None here), so the action is reported OMITTED and
        # conformance rejects an otherwise-correct BT. (RCA 2026-07-09, live 984b161:
        # the worker built a correct MULTIPLE_CHOICE solve but emitted the
        # load-bearing choice + Check clicks as {type:action, tool:"find_and_click"};
        # the sibling-swap accepted the type but the tool-field actions read as
        # "omitted find_and_click".) Treat `tool` as the action ONLY when there is no
        # `action` field AND tool names a registered action (else leave it — `tool`
        # is not otherwise meaningful, but be conservative). Flat params then nest
        # via the action-param block below.
        if "action" not in node and node.get("tool") in KNOWN_ACTIONS:
            node["action"] = node.pop("tool")
            node.setdefault("type", "action")
        # CANONICALIZE a MALFORMED for_each / conditional FIRST. These are
        # LOAD-BEARING composables (a click-loop over N runtime items cannot be
        # unrolled at build time, so unlike extract_question they can't be
        # dropped). EXECUTOR TRUTH (bundle bt_core tick_node VERBATIM, ccm
        # 2026-07-10): type is dispatched against EXACTLY {sequence, fallback,
        # action} — any other type logs "Unknown node type" and returns FAILURE
        # before action is ever read. _tick_action then routes
        # action=="for_each"/"conditional" to the composable handlers, which
        # read items/variable/do/condition/then/else at NODE level. So the ONE
        # executor-native composable shape is {type:'action', action:<name>,
        # <structural keys at node level>}. Serving type='conditional' burned
        # the 2026-07-10 dropdown afternoon twice — first with no action key,
        # then WITH action but type still ='conditional'. Run this BEFORE
        # missing-type inference and action-param-nesting.
        if node.get("action") in ("for_each", "conditional"):
            node["type"] = "action"
        if node.get("type") in ("for_each", "conditional"):
            node.setdefault("action", node["type"])
            node["type"] = "action"
            _p = node.get("params")
            if isinstance(_p, dict):
                for _k in ("items", "variable", "do", "condition", "then", "else"):
                    if _k in _p and _k not in node:
                        node[_k] = _p.pop(_k)
                if not _p:
                    node.pop("params", None)
        # INFER a MISSING node `type` from the node's shape. Worker variance
        # (live RCA 2026-06-15, consult ...23bce8af -> TERMINAL): the worker
        # returns a `tree` root with NO `type` field at all -> _validate_bt
        # rejects ("tree missing 'type'") -> wasted attempt -> 4-tier
        # exhaustion -> terminal, on an EXERCISE_TEXT_INPUT screen that is
        # otherwise handleable. Same class as every other normalization in this
        # function: canonicalize the shape so the BT is valid regardless of
        # whether the worker wrote the structural key. Highest-confidence
        # inferences only (an action field, or the structural keys that uniquely
        # name a composite/control type); ambiguous nodes are left untouched and
        # still fail validation as before.
        if "type" not in node:
            if isinstance(node.get("action"), str):
                node["type"] = "action"
            elif "condition" in node:
                node["type"] = "action"
                node.setdefault("action", "conditional")
            elif "items" in node or "variable" in node:
                node["type"] = "action"
                node.setdefault("action", "for_each")
            elif isinstance(node.get("children"), list):
                node["type"] = "sequence"
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
        # UNWRAP a spurious `args` nesting: worker variance sometimes wraps every
        # node's params one extra level — params:{args:{element:..,strategy:..}} —
        # and the Mac handlers read params.get('element') DIRECTLY, so they get
        # None ('click: element not found'). Same class as the flat-vs-nested
        # variance (633b6b3). Hoist while params is solely {args:<dict>}. (Live RCA
        # 2026-06-14, consult ...2c041273: ALL nodes args-wrapped; nothing flattened it.)
        params = node.get("params")
        while isinstance(params, dict) and set(params.keys()) == {"args"} and isinstance(params["args"], dict):
            node["params"] = params["args"]
            params = node["params"]
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


def _validate_bt(
    parsed: dict,
    consultation_id: str,
    *,
    worker_raw_response_path: str | None = None,
    worker_raw_stdout_path: str | None = None,
) -> None:
    """Validate the parsed BT response has the required shape.

    Server-side JSON Schema owns the worker output envelope. The legacy BT
    checks below remain because the Mac still executes the tree until slotized
    compilers replace worker-authored BTs.
    """
    schema_errors = list(WORKER_OUTPUT_SCHEMA_VALIDATOR.iter_errors(parsed))
    if schema_errors:
        raise BTGenerationError(
            f"BT response for {consultation_id} failed worker output schema: "
            f"{_schema_error_summary(schema_errors)}",
            failure_kind="worker_output_schema_rejection",
            worker_raw_response_path=worker_raw_response_path,
            worker_raw_stdout_path=worker_raw_stdout_path,
            worker_output=parsed,
        )
    if _is_worker_escalate_output(parsed):
        return
    required = ("tree", "screen_type", "expected_next", "extract")
    for k in required:
        if k not in parsed:
            raise BTGenerationError(
                f"BT response for {consultation_id} missing required key: {k!r}",
                failure_kind="validation_rejection",
                worker_raw_response_path=worker_raw_response_path,
                worker_raw_stdout_path=worker_raw_stdout_path,
                worker_output=parsed,
            )
    if not isinstance(parsed["tree"], dict):
        raise BTGenerationError(
            f"BT response for {consultation_id}: 'tree' must be a dict, "
            f"got {type(parsed['tree']).__name__}",
            failure_kind="validation_rejection",
            worker_raw_response_path=worker_raw_response_path,
            worker_raw_stdout_path=worker_raw_stdout_path,
            worker_output=parsed,
        )
    if "type" not in parsed["tree"]:
        root = parsed["tree"]
        if not any(k in root for k in ("action", "children", "condition")):
            capture = (
                f"; captured raw worker response at {worker_raw_response_path}"
                if worker_raw_response_path
                else ""
            )
            raise BTGenerationError(
                f"BT response for {consultation_id}: tree missing 'type' field; "
                f"typeless root has {_root_shape_summary(root)}{capture}",
                failure_kind="validation_rejection",
                worker_raw_response_path=worker_raw_response_path,
                worker_raw_stdout_path=worker_raw_stdout_path,
                worker_output=parsed,
            )
        raise BTGenerationError(
            f"BT response for {consultation_id}: tree missing 'type' field",
            failure_kind="validation_rejection",
            worker_raw_response_path=worker_raw_response_path,
            worker_raw_stdout_path=worker_raw_stdout_path,
            worker_output=parsed,
        )
    # GLOBAL REGISTERED-HANDLER FLOOR (2026-06-15). EVERY action must be a real
    # Mac handler — regardless of artifact kind (the recipe conformance check only
    # runs for YAML artifacts; UNKNOWN-guide BTs were never action-validated, so a
    # hallucinated action reached the Mac and FAILED THERE). RCA: on a hard
    # exercise it couldn't solve, the worker invented `halt`/`escalate_user_assist`
    # to "stop and ask"; `halt` isn't a registered action, so the Mac failed at
    # step 1 and the intended halt+escalate never ran — the wrong-answer SAFE-STOP
    # was itself broken. Rejecting here routes to worker_fallback -> the escalation
    # ladder, which IS the real safe-stop. A genuine stop is the ladder, never a
    # BT action.
    actions = _collect_tree_actions(parsed["tree"])
    unregistered = sorted(a for a in actions if a not in KNOWN_ACTIONS)
    if unregistered:
        raise BTGenerationError(
            f"BT response for {consultation_id} uses unregistered action(s): "
            f"{', '.join(unregistered)} — the Mac has no such handler. The worker "
            f"must never invent actions (e.g. 'halt'/'escalate_user_assist'); a "
            f"genuine stop is the escalation ladder, not a BT action.",
            failure_kind="validation_rejection",
            worker_raw_response_path=worker_raw_response_path,
            worker_raw_stdout_path=worker_raw_stdout_path,
            worker_output=parsed,
        )
    if "_session" in parsed and not isinstance(parsed["_session"], dict):
        raise BTGenerationError(
            f"BT response for {consultation_id}: '_session' must be a dict when present",
            failure_kind="validation_rejection",
            worker_raw_response_path=worker_raw_response_path,
            worker_raw_stdout_path=worker_raw_stdout_path,
            worker_output=parsed,
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

    system_prompt = Path(prompt_meta["system_prompt_path"]).read_text(encoding="utf-8")
    meta = {}
    bt = None
    last_rejection = ""
    attempt = 0
    for attempt in range(1, WORKER_SCHEMA_MAX_ATTEMPTS + 1):
        attempt_instruction = (
            user_instruction
            if attempt == 1
            else _build_retry_instruction(user_instruction, last_rejection)
        )
        if attempt > 1:
            _persist_text_artifact(consult_dir / "user_instruction_retry.txt", attempt_instruction)
        logger.info(
            f"bt_generator: invoking claude for consult={consultation_id} "
            f"(attempt={attempt}/{WORKER_SCHEMA_MAX_ATTEMPTS}, "
            f"timeout={timeout_s}s, budget=${max_budget_usd})"
        )
        try:
            raw_text, meta = call_claude_cli(
                system_prompt=system_prompt,
                user_message=attempt_instruction,
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

        worker_raw_stdout_path = None
        worker_raw_response_path = None
        try:
            inner_text = _extract_json_object(raw_text)
        except ValueError as e:
            worker_raw_stdout_path = _persist_text_artifact(
                consult_dir / WORKER_RAW_STDOUT_NAME,
                raw_text,
            )
            rejection = BTGenerationError(
                f"BT JSON for {consultation_id} extraction failed: {e}; "
                f"head: {raw_text[:300]}",
                failure_kind="worker_output_schema_rejection",
                worker_raw_stdout_path=worker_raw_stdout_path,
            )
        else:
            try:
                bt = json.loads(inner_text)
            except json.JSONDecodeError as e:
                worker_raw_stdout_path = _persist_text_artifact(
                    consult_dir / WORKER_RAW_STDOUT_NAME,
                    raw_text,
                )
                rejection = BTGenerationError(
                    f"BT JSON for {consultation_id} parse failed: {e}; head: {inner_text[:300]}",
                    failure_kind="worker_output_schema_rejection",
                    worker_raw_stdout_path=worker_raw_stdout_path,
                )
            else:
                worker_raw_response_path = _persist_json_artifact(
                    consult_dir / WORKER_RAW_RESPONSE_NAME,
                    bt,
                )

                if isinstance(bt, dict):
                    bt = _canonical_worker_output(bt)
                    if isinstance(bt.get("tree"), list):
                        bt["tree"] = {"type": "sequence", "children": bt["tree"]}

                    # Correct historical dialect variance only; new dialect repairs
                    # must be schema-retried, not added to the normalizer.
                    if not _is_worker_escalate_output(bt):
                        _normalize_bt_nodes(bt.get("tree", bt))

                try:
                    _validate_bt(
                        bt,
                        consultation_id,
                        worker_raw_response_path=worker_raw_response_path,
                        worker_raw_stdout_path=worker_raw_stdout_path,
                    )
                    if _is_worker_escalate_output(bt):
                        rejection = _worker_escalate_error(
                            bt,
                            consultation_id,
                            worker_raw_response_path=worker_raw_response_path,
                            worker_raw_stdout_path=worker_raw_stdout_path,
                        )
                        rejection.rejected_bt_path = _persist_json_artifact(
                            consult_dir / WORKER_ESCALATE_NAME,
                            bt,
                        )
                        raise rejection
                    validate_worker_bt_response(bt, platform=platform, screen_type=context["screen_type"])
                except ScreenTypeAssemblerError as e:
                    rejection = BTGenerationError(
                        f"BT recipe-conformance validation failed for {consultation_id}: {e}",
                        failure_kind="conformance_rejection",
                        worker_raw_response_path=worker_raw_response_path,
                        worker_raw_stdout_path=worker_raw_stdout_path,
                        worker_output=bt,
                    )
                except BTGenerationError as e:
                    rejection = e
                else:
                    break

        last_rejection = str(rejection)
        _persist_rejection_artifact(
            consult_dir / f"worker_rejection_attempt{attempt}.json",
            rejection,
        )
        if rejection.failure_kind == WORKER_ESCALATE_KIND:
            raise rejection
        if attempt < WORKER_SCHEMA_MAX_ATTEMPTS:
            logger.warning(
                "bt_generator: worker output rejected for %s attempt %d/%d: %s",
                consultation_id,
                attempt,
                WORKER_SCHEMA_MAX_ATTEMPTS,
                last_rejection,
            )
            continue
        if not rejection.rejected_bt_path:
            rejection.rejected_bt_path = _persist_rejection_artifact(
                consult_dir / "rejected_bt.json",
                rejection,
            )
        raise rejection

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
        f"(schema_attempts={attempt}) "
        f"(turns={meta['num_turns']}, api-equivalent cost "
        f"${meta['total_cost_usd']:.3f}, "
        f"screen_type={bt.get('screen_type')}, root_type={bt['tree'].get('type')})"
    )
    return bt
