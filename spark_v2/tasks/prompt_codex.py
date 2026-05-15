"""Prompt assembly helpers for spark_v2."""

from __future__ import annotations

import copy
import json
import logging
import re
from functools import lru_cache
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

CONSULTATIONS_DIR = Path("/home/user/taey-ed/consultations")
WORKER_PROMPT_SPEC = CONSULTATIONS_DIR / "CLAUDE_CLI_WORKER_PROMPT_v1.md"
UNIVERSAL_LAYER_PATH = CONSULTATIONS_DIR / "UNIVERSAL_LAYER_v1.md"

IDENTITY_BLOCK = "\n".join(
    [
        "You are Taey-Ed's behavior-tree generator for browser automation.",
        "Your job: emit ONE executable BT JSON describing the next action on this screen.",
        "",
        "Hard rules:",
        "- JSON only. No prose preamble. No markdown fences. First char { , last char } .",
        "- Reason internally; emit JSON. Internal narration MUST NOT leak into output.",
        "- Honor the auditable-intention output schema (Section 7 below) on EVERY response.",
        "- When in doubt, screen_type=UNKNOWN with empty tree. NEVER guess.",
    ]
)


def _read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _extract_section(markdown: str, section_number: int) -> str:
    pattern = rf"^## Section {section_number} — .*$"
    match = re.search(pattern, markdown, flags=re.MULTILINE)
    if not match:
        raise ValueError(f"Section {section_number} not found in Universal Layer")
    start = match.end()
    next_match = re.search(r"^## Section \d+ — .*$", markdown[start:], flags=re.MULTILINE)
    end = start + next_match.start() if next_match else len(markdown)
    return markdown[start:end].strip()


def _strip_provenance_notes(text: str) -> str:
    lines: list[str] = []
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("*Source*"):
            continue
        if stripped.startswith("**Signatures empirically verified**"):
            continue
        if stripped.startswith("**Empirically verified**"):
            continue
        lines.append(line.rstrip())
    return "\n".join(lines).strip()


@lru_cache(maxsize=4)
def load_universal_layer_sections(path: str) -> dict[str, str]:
    markdown = _read_text(Path(path))
    principles = _strip_provenance_notes(_extract_section(markdown, 2))
    handlers = _strip_provenance_notes(_extract_section(markdown, 3))
    output_schema = _strip_provenance_notes(_extract_section(markdown, 7))
    return {
        "principles": principles,
        "handlers": handlers,
        "output_schema": output_schema,
    }


def _drop_provenance(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: _drop_provenance(child)
            for key, child in value.items()
            if key != "provenance"
        }
    if isinstance(value, list):
        return [_drop_provenance(item) for item in value]
    return value


def _is_populated(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, (list, tuple, set, dict)):
        return len(value) > 0
    return True


def _json_block(value: Any) -> str:
    return json.dumps(value, indent=2, ensure_ascii=True, sort_keys=True)


def format_platform_knowledge(platform_data: dict) -> str:
    compact = _drop_provenance(copy.deepcopy(platform_data or {}))
    platform = compact.get("platform", {})
    display_name = platform.get("display_name") or platform.get("name") or "unknown"

    blocks: list[str] = []
    global_data = compact.get("global", {})
    global_lines: list[str] = []
    for key in (
        "completion_indicators",
        "advancement_link_patterns",
        "video_completion_signal",
        "timing_characteristics",
    ):
        value = global_data.get(key)
        if _is_populated(value):
            global_lines.append(f"- {key}:\n{_json_block(value)}")
    if global_lines:
        blocks.append("Platform-specific signals:\n" + "\n".join(global_lines))

    screen_patterns = {
        key: value
        for key, value in compact.get("screen_patterns", {}).items()
        if _is_populated(value)
    }
    if screen_patterns:
        rendered = [f"- {key}:\n{_json_block(value)}" for key, value in screen_patterns.items()]
        blocks.append("Screen-type specifics:\n" + "\n".join(rendered))

    never_clicks = compact.get("never_clicks_platform", [])
    if _is_populated(never_clicks):
        blocks.append("Platform-specific never-click additions:\n" + _json_block(never_clicks))

    widget_classes = compact.get("widget_classes", {})
    if _is_populated(widget_classes):
        blocks.append("Known widget mechanics:\n" + _json_block(widget_classes))

    cached_bts = compact.get("cached_bts", {})
    if _is_populated(cached_bts):
        blocks.append("Cached behavior-tree metadata:\n" + _json_block(cached_bts))

    if not blocks:
        return f"Platform: {display_name} — first encounter, no learned patterns yet."
    return f"Platform: {display_name}\n" + "\n\n".join(blocks)


def format_provisional(provisional_data: dict | None) -> str:
    if not provisional_data:
        return ""
    compact = _drop_provenance(copy.deepcopy(provisional_data))
    return "PROVISIONAL — empirical validation pending:\n" + format_platform_knowledge(compact)


def format_reconsult_context(last_result: dict | None, tier: int) -> str:
    if not last_result:
        return ""
    lines = [f"TIER {tier} RECONSULT — previous attempt did not advance the screen."]
    failed_bt = last_result.get("failed_bt")
    if failed_bt:
        lines.extend(
            [
                "",
                "Failed BT (the one that did not advance the screen):",
                _json_block(failed_bt),
            ]
        )
    bt_debug_tail = last_result.get("bt_debug_tail")
    if bt_debug_tail:
        lines.extend(["", "BT execution log tail:", bt_debug_tail])
    user_response = last_result.get("user_response")
    if user_response:
        lines.extend(["", "User guidance:", user_response])
    lines.extend(
        [
            "",
            "Build a FUNDAMENTALLY DIFFERENT BT. Do NOT tweak parameters. If no",
            "alternative is plausible from the tree, emit screen_type=UNKNOWN with",
            "an empty tree.",
        ]
    )
    return "\n".join(lines).strip()


def _find_web_area(node: Any) -> dict | None:
    if not isinstance(node, dict):
        return None
    if node.get("role") == "AXWebArea":
        return node
    for child in node.get("children", []):
        found = _find_web_area(child)
        if found is not None:
            return found
    return None


def _prune_node(node: Any) -> Any:
    if not isinstance(node, dict):
        return node
    if node.get("role") in {"AXMenuBar", "AXToolbar", "AXTabGroup"}:
        return None
    pruned: dict[str, Any] = {}
    for key, value in node.items():
        if key == "children":
            children = []
            for child in value or []:
                kept = _prune_node(child)
                if kept is not None:
                    children.append(kept)
            if children:
                pruned[key] = children
            continue
        if key in {
            "role",
            "name",
            "title",
            "description",
            "value",
            "selected",
            "focused",
            "enabled",
            "visible_bbox",
            "element_id",
        }:
            if _is_populated(value) or isinstance(value, bool):
                pruned[key] = value
    if not pruned.get("children") and not any(
        key in pruned for key in ("name", "title", "description", "value")
    ):
        role = pruned.get("role")
        if role not in {"AXButton", "AXLink", "AXTextField", "AXTextArea", "AXCheckBox", "AXRadioButton", "AXComboBox"}:
            return None
    return pruned


def prune_ax_tree(tree: dict, target_chars: int = 50000) -> dict:
    web_area = _find_web_area(tree)
    working = web_area if web_area is not None else tree
    pruned = _prune_node(working) or {}
    raw = json.dumps(pruned, ensure_ascii=True)
    if len(raw) <= target_chars:
        return pruned

    def shrink(node: Any, depth: int = 0) -> Any:
        if not isinstance(node, dict):
            return node
        compact = {
            key: value
            for key, value in node.items()
            if key != "children" and key not in {"description", "value"} | ({"name", "title"} if depth > 10 else set())
        }
        children = [shrink(child, depth + 1) for child in node.get("children", [])[:20]]
        children = [child for child in children if child]
        if children:
            compact["children"] = children
        return compact

    aggressive = shrink(pruned)
    aggressive_text = json.dumps(aggressive, ensure_ascii=True)
    logger.warning(
        "prompt_codex: aggressive AX tree pruning from %d chars to %d chars",
        len(raw),
        len(aggressive_text),
    )
    aggressive["_notes"] = f"AX_TREE_PRUNED_FROM_{len(raw)}"
    return aggressive


def _exercise_like_context(screen_context: dict) -> bool:
    marker = str(screen_context.get("screen_type_hint") or "").upper()
    if marker.startswith("EXERCISE"):
        return True
    tree_blob = json.dumps(screen_context.get("tree", {}), ensure_ascii=True)
    for token in ('"AXRadioButton"', '"AXCheckBox"', '"AXTextField"', '"AXComboBox"'):
        if token in tree_blob:
            return True
    return False


def assemble_system_prompt(
    *,
    universal_sections: dict[str, str],
    platform_data: dict,
    provisional_data: dict | None,
    last_result: dict | None,
    tier: int,
) -> str:
    blocks = [
        ("identity", IDENTITY_BLOCK),
        ("principles", universal_sections["principles"]),
        ("handlers", universal_sections["handlers"]),
        ("output_schema", universal_sections["output_schema"]),
        ("platform_knowledge", format_platform_knowledge(platform_data)),
    ]
    provisional_block = format_provisional(provisional_data)
    if provisional_block:
        blocks.append(("provisional", provisional_block))
    reconsult_block = format_reconsult_context(last_result, tier)
    if reconsult_block:
        blocks.append(("reconsult", reconsult_block))

    for label, text in blocks:
        logger.info("prompt_codex: system block %s size=%d", label, len(text))

    system_prompt = "\n\n".join(text for _, text in blocks if text)
    if len(system_prompt) > 25000:
        logger.warning("prompt_codex: system prompt size=%d exceeds 25000 chars", len(system_prompt))
    return system_prompt


def assemble_user_message(
    *,
    platform_display_name: str,
    current_url: str | None,
    last_screen_type: str | None,
    tier: int,
    course_id: str | None,
    tree: dict,
    screenshot_path: str,
    relevant_kb_chunks: list[dict] | None,
    screen_context: dict,
) -> str:
    pruned_tree = prune_ax_tree(tree)
    tree_text = _json_block(pruned_tree)

    header_lines = [
        f"Platform: {platform_display_name}",
        f"Screen URL: {current_url or 'unavailable'}",
        f"Last known screen_type: {last_screen_type or 'none'}",
        f"Consultation tier: {tier if tier else 'fresh'}",
        f"Course (if known): {course_id or 'unknown'}",
    ]

    sections = [
        "\n".join(header_lines),
        "Section A — AX Tree\n" + tree_text,
        f"Section B — Screenshot\nScreenshot: {screenshot_path}",
    ]

    if relevant_kb_chunks and _exercise_like_context(screen_context):
        chunk_lines = ["RELEVANT REFERENCE MATERIAL (retrieved from this course's captured content):"]
        for chunk in relevant_kb_chunks:
            text = str(chunk.get("text", "")).strip()
            if not text:
                continue
            chunk_lines.append(text)
            chunk_lines.append("---")
        if len(chunk_lines) > 1:
            if chunk_lines[-1] == "---":
                chunk_lines.pop()
            sections.append("Section C — Relevant KB Chunks\n" + "\n".join(chunk_lines))

    sections.append(
        "Section D — Closing directive\n"
        "Emit ONE JSON object conforming to the Output Schema in your system prompt.\n"
        "Return JSON only."
    )

    user_message = "\n\n".join(sections)
    logger.info("prompt_codex: user message size=%d", len(user_message))
    if len(user_message) > 50000:
        logger.warning("prompt_codex: user message size=%d exceeds 50000 chars", len(user_message))
    return user_message


def compile_prompt(
    platform: str,
    screen_context: dict,
    universal_sections: dict[str, str] | None = None,
    platform_data: dict | None = None,
    provisional_data: dict | None = None,
) -> str:
    """Compatibility wrapper for callers that only need the system prompt."""
    sections = universal_sections or load_universal_layer_sections(str(UNIVERSAL_LAYER_PATH))
    return assemble_system_prompt(
        universal_sections=sections,
        platform_data=platform_data or {},
        provisional_data=provisional_data,
        last_result=screen_context.get("last_result"),
        tier=int(screen_context.get("tier") or 0),
    )
