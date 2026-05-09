"""
Behavior Tree Engine Core - Node execution and entry point.

This is the core engine that executes behavior trees from YAML.
All workflow logic lives in YAML. This file is pure execution machinery.

Node types:
  sequence  - Run children in order, fail on first failure
  fallback  - Run children in order, succeed on first success
  action    - Leaf node, calls a registered handler
  for_each  - Iterate list, execute subtree per item
  conditional - If/else on blackboard value
"""

import logging
import time
from typing import Any, Dict, List, Optional, Callable

logger = logging.getLogger("taey-ed")

# File logger for debugging - readable via SSH
_bt_log = open("/tmp/behavior_tree_debug.log", "a", encoding="utf-8")
def btlog(msg):
    _bt_log.write(f"{time.strftime('%H:%M:%S')} {msg}\n")
    _bt_log.flush()
    logger.info(msg)


# =========================================================================
# Status
# =========================================================================

class Status:
    SUCCESS = "success"
    FAILURE = "failure"


# =========================================================================
# Blackboard - shared variable store
# =========================================================================

class Blackboard:
    def __init__(self):
        self._data: Dict[str, Any] = {}

    def set(self, key: str, value: Any):
        self._data[key] = value
        logger.debug(f"BB SET: {key} = {type(value).__name__}")

    def get(self, key: str, default=None) -> Any:
        return self._data.get(key, default)

    def resolve(self, value: Any) -> Any:
        """
        Resolve $variable references.

        $var          -> blackboard["var"]
        $var.field    -> blackboard["var"]["field"]
        $var.field.x  -> nested access
        Non-$ strings returned as-is.
        """
        if not isinstance(value, str) or not value.startswith("$"):
            return value

        path = value[1:].split(".")
        result = self._data.get(path[0])

        for part in path[1:]:
            if result is None:
                return None
            if isinstance(result, dict):
                result = result.get(part)
            elif isinstance(result, list) and part.isdigit():
                idx = int(part)
                result = result[idx] if idx < len(result) else None
            else:
                return None
        return result

    def resolve_params(self, params: dict) -> dict:
        """Resolve all $variable references in a params dict."""
        resolved = {}
        for key, val in params.items():
            if isinstance(val, str):
                resolved[key] = self.resolve(val)
            elif isinstance(val, list):
                resolved[key] = [self.resolve(v) if isinstance(v, str) else v for v in val]
            elif isinstance(val, dict):
                resolved[key] = self.resolve_params(val)
            else:
                resolved[key] = val
        return resolved

    def snapshot(self) -> dict:
        """Return keys for debugging (not full data - elements aren't serializable)."""
        return {k: type(v).__name__ for k, v in self._data.items()}


# =========================================================================
# Execution Context
# =========================================================================

class ExecutionContext:
    def __init__(self, app_name: str, platform: str, course_id: str,
                 extract_config: dict = None, stop_event=None):
        self.app_name = app_name
        self.platform = platform
        self.course_id = course_id
        self.extract_config = extract_config or {}
        self.stop_event = stop_event
        self.blackboard = Blackboard()
        self._handlers: Dict[str, Callable] = {}

    def register(self, name: str, handler: Callable):
        self._handlers[name] = handler

    def get_handler(self, name: str) -> Optional[Callable]:
        return self._handlers.get(name)


# =========================================================================
# Tree Nodes
# =========================================================================

def tick_node(node_def: dict, ctx: ExecutionContext) -> str:
    """
    Execute a single node from its YAML definition.
    Recursive - composite nodes call tick_node on children.
    """
    node_type = node_def.get("type", "action")

    if node_type == "sequence":
        return _tick_sequence(node_def, ctx)
    elif node_type == "fallback":
        return _tick_fallback(node_def, ctx)
    elif node_type == "action":
        return _tick_action(node_def, ctx)
    else:
        logger.error(f"Unknown node type: {node_type}")
        return Status.FAILURE


def _tick_sequence(node_def: dict, ctx: ExecutionContext) -> str:
    """Run children in order. Fail on first failure. Halt on continue_loop."""
    children = node_def.get("children", [])
    for i, child in enumerate(children):
        summary = _node_summary(child)
        btlog(f"seq step {i+1}/{len(children)}: {summary}")
        status = tick_node(child, ctx)
        if status == Status.FAILURE:
            btlog(f"seq FAILED at step {i+1}: {summary}")
            return Status.FAILURE
        # HALT AXIOM: If a child signals continue_loop (e.g., video_poll),
        # stop the sequence immediately. Don't execute remaining children.
        if ctx.blackboard.get("_continue_loop"):
            btlog(f"seq HALTED at step {i+1}: _continue_loop set by {summary}")
            return Status.SUCCESS
    return Status.SUCCESS


def _tick_fallback(node_def: dict, ctx: ExecutionContext) -> str:
    """Run children in order. Succeed on first success."""
    children = node_def.get("children", [])
    for i, child in enumerate(children):
        logger.info(f"  fallback trying {i+1}/{len(children)}: {_node_summary(child)}")
        status = tick_node(child, ctx)
        if status == Status.SUCCESS:
            return Status.SUCCESS
    logger.warning("  fallback: all options failed")
    return Status.FAILURE


def _tick_action(node_def: dict, ctx: ExecutionContext) -> str:
    """Execute a leaf action or composable (for_each, conditional)."""
    action_name = node_def.get("action", "")

    # Composable: for_each
    if action_name == "for_each":
        return _tick_for_each(node_def, ctx)

    # Composable: conditional
    if action_name == "conditional":
        return _tick_conditional(node_def, ctx)

    # Regular action - look up handler
    handler = ctx.get_handler(action_name)
    if handler is None:
        logger.error(f"No handler: {action_name}")
        return Status.FAILURE

    # Resolve params
    raw_params = node_def.get("params", {})
    params = ctx.blackboard.resolve_params(raw_params)

    # Also resolve any direct $refs in params values that are element refs
    # (e.g., element: $_current needs to resolve to the actual dict)
    for key, val in list(params.items()):
        if val is None and raw_params.get(key, "").startswith("$"):
            # Re-resolve directly
            params[key] = ctx.blackboard.resolve(raw_params[key])

    # Honor pre_delay / post_delay centrally so handlers don't each have to.
    # Knowledge.json + Spark prompts both reference these — silently ignoring
    # them was a contract drift caught during the Wonder Blocks audit.
    pre_delay = params.pop("pre_delay", None)
    post_delay = params.pop("post_delay", None)
    if pre_delay is not None:
        try:
            time.sleep(float(pre_delay))
        except (TypeError, ValueError):
            btlog(f"invalid pre_delay ignored: {pre_delay}")

    try:
        result = handler(ctx, params)
    except Exception as e:
        btlog(f"ACTION EXCEPTION {action_name}: {e}")
        logger.error(f"Action {action_name} raised: {e}", exc_info=True)
        return Status.FAILURE

    if result is None:
        btlog(f"ACTION RETURNED NONE: {action_name}")
        return Status.FAILURE

    # Store result
    store_key = node_def.get("store")
    if store_key:
        ctx.blackboard.set(store_key, result)

    # Store to current loop element's attribute
    store_to_current = node_def.get("store_to_current")
    if store_to_current:
        current = ctx.blackboard.get("_current")
        if isinstance(current, dict):
            current[store_to_current] = result

    # Propagate continue_loop to blackboard (video_poll etc.)
    # This ensures execute_tree can detect it even without store: in YAML
    if isinstance(result, dict) and result.get("continue_loop"):
        ctx.blackboard.set("_continue_loop", True)

    # Check for explicit failure
    if isinstance(result, dict) and result.get("success") is False:
        btlog(f"ACTION success=False: {action_name} result_keys={list(result.keys())}")
        return Status.FAILURE

    # Post-delay honored centrally (paired with pre_delay above).
    if post_delay is not None:
        try:
            time.sleep(float(post_delay))
        except (TypeError, ValueError):
            btlog(f"invalid post_delay ignored: {post_delay}")

    return Status.SUCCESS


def _tick_for_each(node_def: dict, ctx: ExecutionContext) -> str:
    """Iterate over a list, execute subtree per item. Preserves outer scope."""
    items_ref = node_def.get("items", "")
    items = ctx.blackboard.resolve(items_ref)

    if not items or not isinstance(items, list):
        btlog(f"for_each: no items from {items_ref} (type={type(items).__name__}, val={items})")
        return Status.FAILURE

    do_node = node_def.get("do", {})
    variable = node_def.get("variable", "_current")

    # Save outer scope to restore after loop (nested for_each safety)
    prev_variable = ctx.blackboard.get(variable)
    prev_current = ctx.blackboard.get("_current")
    prev_index = ctx.blackboard.get("_index")

    btlog(f"for_each: {len(items)} items from {items_ref}")
    try:
        for i, item in enumerate(items):
            ctx.blackboard.set(variable, item)
            ctx.blackboard.set("_current", item)
            ctx.blackboard.set("_index", i)

            status = tick_node(do_node, ctx)
            if status == Status.FAILURE:
                btlog(f"for_each FAILED at item {i+1}/{len(items)}")
                return Status.FAILURE
    finally:
        # Restore outer scope
        ctx.blackboard.set(variable, prev_variable)
        ctx.blackboard.set("_current", prev_current)
        ctx.blackboard.set("_index", prev_index)

    return Status.SUCCESS


def _tick_conditional(node_def: dict, ctx: ExecutionContext) -> str:
    """If condition truthy, execute then branch. Else execute else branch."""
    condition_ref = node_def.get("condition", "")
    value = ctx.blackboard.resolve(condition_ref)

    # Normalize string booleans: "false"/"False"/"FALSE" -> False,
    # "true"/"True"/"TRUE" -> True. Prevents Python's truthy-string trap.
    if isinstance(value, str):
        if value.lower() == "false":
            value = False
        elif value.lower() == "true":
            value = True
        # Other non-empty strings remain truthy (intentional)

    if value:
        then_node = node_def.get("then")
        if then_node:
            return tick_node(then_node, ctx)
        return Status.SUCCESS
    else:
        else_node = node_def.get("else")
        if else_node:
            return tick_node(else_node, ctx)
        return Status.SUCCESS  # No else = skip, not fail


def _node_summary(node_def: dict) -> str:
    """One-line summary of a node for logging."""
    ntype = node_def.get("type", "action")
    if ntype == "action":
        action = node_def.get("action", "?")
        params = node_def.get("params", {})
        target = params.get("target", params.get("role", ""))
        return f"{action}({target})" if target else action
    return ntype


# =========================================================================
# Main Entry Point
# =========================================================================

def execute_tree(
    tree_definition: dict,
    app_name: str,
    platform: str,
    course_id: str = "unknown",
    extract_config: dict = None,
    stop_event=None,
) -> dict:
    """
    Execute a behavior tree from YAML definition.

    Called by pipeline.py when a screen match includes a 'tree:' section.

    Returns:
        {
            "success": True/False,
            "action": "behavior_tree (status)",
            "continue_loop": True/False,
        }
    """
    from app.tasks.bt_handlers import register_all_handlers

    ctx = ExecutionContext(
        app_name=app_name,
        platform=platform,
        course_id=course_id,
        extract_config=extract_config,
        stop_event=stop_event,
    )
    register_all_handlers(ctx)

    btlog(f"=== Behavior Tree START for {platform} ===")
    status = tick_node(tree_definition, ctx)
    btlog(f"=== Behavior Tree END: {status} ===")

    # Check for continue_loop (video_poll etc.)
    # _continue_loop is set directly by _tick_action when any handler returns it
    continue_loop = bool(ctx.blackboard.get("_continue_loop", False))

    return {
        "success": status == Status.SUCCESS,
        "action": f"behavior_tree ({status})",
        "continue_loop": continue_loop,
    }
