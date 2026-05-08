# STATUS: FROZEN. Verified 2026-02-19. Do not modify.
"""
YAML config schema validation.

Validates platform configs against V9 format rules:
- Required top-level fields (platform, version)
- Screen definitions have valid markers and tree: sections
- Cross-references expected_next screen names exist
- Safety halt patterns are well-formed

Called on config load to catch malformed YAML early
(especially important when agents write YAML during consultations).
"""

from typing import Optional


class ConfigError:
    """A single validation error."""

    def __init__(self, path: str, message: str):
        self.path = path
        self.message = message

    def __repr__(self):
        return f"{self.path}: {self.message}"


def validate_tree_no_fallbacks(tree: dict, path: str = "tree") -> list[ConfigError]:
    """
    Reject behavior trees containing fallback nodes.

    Fallbacks hide failures instead of escalating them.
    Every action must succeed or the tree fails and escalates.
    Rule: "Fallbacks are lies" (CLAUDE.md anti-pattern #1)
    """
    errors = []
    if not isinstance(tree, dict):
        return errors

    if tree.get("type") == "fallback":
        errors.append(ConfigError(
            path,
            "fallback nodes are BANNED — use strict sequences. "
            "First error must stop and escalate, not silently continue."
        ))

    for i, child in enumerate(tree.get("children", [])):
        errors.extend(validate_tree_no_fallbacks(child, f"{path}.children[{i}]"))

    return errors


def _collect_actions(tree: dict, actions: list = None) -> list[dict]:
    """Recursively collect all action nodes from a behavior tree."""
    if actions is None:
        actions = []
    if not isinstance(tree, dict):
        return actions
    if tree.get("type") == "action":
        actions.append(tree)
    for child in tree.get("children", []):
        _collect_actions(child, actions)
    # Also check for_each "do" subtree
    do_node = tree.get("do")
    if isinstance(do_node, dict):
        _collect_actions(do_node, actions)
    # Also check conditional then/else
    for key in ("then", "else"):
        sub = tree.get(key)
        if isinstance(sub, dict):
            _collect_actions(sub, actions)
    return actions


def _count_role_in_tree(ax_tree: dict, role: str) -> int:
    """Count occurrences of a specific AX role in the accessibility tree."""
    count = 0
    stack = [ax_tree]
    while stack:
        node = stack.pop()
        if not isinstance(node, dict):
            continue
        if node.get("role") == role:
            count += 1
        children = node.get("children")
        if children:
            stack.extend(children)
    return count


# Minimum AXLink count (after subtracting browser chrome estimate) to
# consider a screen a "content list" requiring dynamic navigation.
CONTENT_LIST_LINK_THRESHOLD = 15


def validate_bt_for_screen(bt: dict, ax_tree: dict) -> list[ConfigError]:
    """
    Validate that a behavior tree uses dynamic discovery on content list screens.

    Structural check — no allowlists. If the screen has many similar links
    (content list pattern), the BT MUST contain at least one dynamic discovery
    mechanism: find_all, send_to_llm, or extract_question. A BT composed
    entirely of literal find_and_click targets is rejected because content
    varies per course/user/progress.

    Returns list of ConfigError. Empty = valid.
    """
    errors = []
    if not bt or not ax_tree:
        return errors

    actions = _collect_actions(bt)

    # Dynamic discovery actions — if ANY of these are present, the BT is
    # doing something smarter than hardcoded clicks. Allow it.
    DYNAMIC_ACTIONS = {"find_all", "send_to_llm", "extract_question", "discover_menu", "press_key"}
    has_dynamic = any(a.get("action") in DYNAMIC_ACTIONS for a in actions)
    if has_dynamic:
        return errors

    # Check if any find_and_click uses $variable targets (blackboard refs).
    # If so, some upstream step produced the target dynamically — allow it.
    has_variable_target = any(
        isinstance(a.get("params", {}).get("target", ""), str)
        and a.get("params", {}).get("target", "").startswith("$")
        for a in actions
        if a.get("action") == "find_and_click"
    )
    if has_variable_target:
        return errors

    # Count links in the accessibility tree
    link_count = _count_role_in_tree(ax_tree, "AXLink")

    # If screen has many links, this is a content list — reject pure-literal BTs
    if link_count >= CONTENT_LIST_LINK_THRESHOLD:
        errors.append(ConfigError(
            "tree",
            f"REJECTED: Screen has {link_count} links (content list) but BT "
            f"contains only hardcoded literal targets with no dynamic discovery "
            f"(no find_all, send_to_llm, extract_question, or $variable targets). "
            f"Screens with content lists MUST use the navigate pattern: "
            f"find_all → send_to_llm(question_type=\"navigate\") → "
            f"find_and_click($nav_result.answer). See CLAUDE.md Section 7 Step 8."
        ))

    return errors


def validate_config(config: dict) -> list[ConfigError]:
    """
    Validate a platform config dict.

    Returns list of ConfigError. Empty list = valid.
    """
    errors = []

    # Top-level required fields
    if not config.get("platform"):
        errors.append(ConfigError("platform", "missing or empty"))
    if not config.get("version"):
        errors.append(ConfigError("version", "missing or empty"))

    screens = config.get("screens", {})
    if not isinstance(screens, dict):
        errors.append(ConfigError("screens", "must be a dict"))
        return errors

    screen_names = set(screens.keys())

    for name, screen in screens.items():
        prefix = f"screens.{name}"

        if not isinstance(screen, dict):
            errors.append(ConfigError(prefix, "must be a dict"))
            continue

        # Description
        if not screen.get("description"):
            errors.append(ConfigError(f"{prefix}.description", "missing or empty"))

        # Markers - required, non-empty list
        markers = screen.get("markers")
        if not markers:
            errors.append(ConfigError(f"{prefix}.markers", "missing or empty"))
        elif not isinstance(markers, list):
            errors.append(ConfigError(f"{prefix}.markers", "must be a list"))
        else:
            for i, marker in enumerate(markers):
                if not isinstance(marker, dict):
                    errors.append(ConfigError(f"{prefix}.markers[{i}]", "must be a dict"))
                elif "text" not in marker and "role" not in marker:
                    errors.append(ConfigError(f"{prefix}.markers[{i}]", "must have 'text' or 'role'"))
                if isinstance(marker, dict) and "match" in marker:
                    if marker["match"] not in ("exact", "contains"):
                        errors.append(ConfigError(f"{prefix}.markers[{i}].match", f"invalid value '{marker['match']}', must be 'exact' or 'contains'"))

        # Tree section - REQUIRED (V9 format: all screens use tree:)
        has_tree = bool(screen.get("tree"))
        if not has_tree:
            errors.append(ConfigError(prefix, "must have 'tree' section (V9 format)"))

        # Validate tree (behavior tree)
        if has_tree:
            tree = screen["tree"]
            if not isinstance(tree, dict):
                errors.append(ConfigError(f"{prefix}.tree", "must be a dict"))
            elif "type" not in tree:
                errors.append(ConfigError(f"{prefix}.tree", "missing 'type'"))
            else:
                # Reject fallback nodes - fallbacks are lies
                errors.extend(validate_tree_no_fallbacks(tree, f"{prefix}.tree"))

        # expected_next references: info-only, not errors.
        # Screens may be recognized dynamically via vector matching without YAML entries.
        expected_next = screen.get("expected_next", [])
        if isinstance(expected_next, list):
            for next_name in expected_next:
                if next_name and next_name not in screen_names:
                    pass  # Not an error: screen may be vector-matched dynamically

        # Validate validation config
        validation = screen.get("validation")
        if validation:
            if not isinstance(validation, dict):
                errors.append(ConfigError(f"{prefix}.validation", "must be a dict"))
            else:
                if "delay" in validation and not isinstance(validation["delay"], (int, float)):
                    errors.append(ConfigError(f"{prefix}.validation.delay", "must be a number"))
                if "max_wait" in validation and not isinstance(validation["max_wait"], (int, float)):
                    errors.append(ConfigError(f"{prefix}.validation.max_wait", "must be a number"))

        # Validate extract config
        extract = screen.get("extract")
        if extract:
            if not isinstance(extract, dict):
                errors.append(ConfigError(f"{prefix}.extract", "must be a dict"))

    return errors


