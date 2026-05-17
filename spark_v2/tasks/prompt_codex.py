"""Prompt assembly helpers for spark_v2."""

from __future__ import annotations

import copy
import json
import logging
import re
from functools import lru_cache
from pathlib import Path
from typing import Any

from spark_v2.tasks.knowledge_loader import iter_provisional_entries

logger = logging.getLogger(__name__)

CONSULTATIONS_DIR = Path("/home/user/taey-ed/consultations")
UNIVERSAL_LAYER_PATH = CONSULTATIONS_DIR / "UNIVERSAL_LAYER_v1.md"
WORKER_PROMPT_SPEC = CONSULTATIONS_DIR / "CLAUDE_CLI_WORKER_PROMPT_v1.md"

HARD_TREE_CHAR_LIMIT = 200000


class AXTreeTooLargeError(RuntimeError):
    """Raised when the AX tree cannot be reduced to a safe prompt size."""


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


def _extract_named_section(markdown: str, heading: str, level: int = 3) -> str:
    pattern = rf"^{'#' * level} {re.escape(heading)}$"
    match = re.search(pattern, markdown, flags=re.MULTILINE)
    if not match:
        raise ValueError(f"Section heading not found: {heading}")
    start = match.end()
    next_match = re.search(rf"^#{{1,{level}}} .*$", markdown[start:], flags=re.MULTILINE)
    end = start + next_match.start() if next_match else len(markdown)
    return markdown[start:end].strip()


def _extract_fenced_block(text: str, language: str | None = None) -> str:
    if language:
        pattern = rf"```{re.escape(language)}\n(.*?)\n```"
    else:
        pattern = r"```(?:\w+)?\n(.*?)\n```"
    match = re.search(pattern, text, flags=re.DOTALL)
    if not match:
        raise ValueError("Fenced block not found")
    return match.group(1).strip()


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
    identity = _strip_provenance_notes(_extract_named_section(markdown, "Section 1.5 — Identity Contract", level=2))
    principles = _strip_provenance_notes(_extract_section(markdown, 2))
    handlers = _strip_provenance_notes(_extract_section(markdown, 3))
    output_schema = _strip_provenance_notes(_extract_section(markdown, 7))
    return {
        "identity": identity,
        "principles": principles,
        "handlers": handlers,
        "output_schema": output_schema,
    }


@lru_cache(maxsize=2)
def load_onboarding_messages(path: str = str(UNIVERSAL_LAYER_PATH)) -> dict[str, str]:
    markdown = _read_text(Path(path))
    section = _extract_named_section(markdown, "Canonical Message Templates", level=3)
    parsed = json.loads(_extract_fenced_block(section, "json"))
    onboarding_message = str(parsed.get("onboarding_message") or "").strip()
    discovery_message = str(parsed.get("discovery_in_progress_message") or "").strip()
    if not onboarding_message or not discovery_message:
        raise ValueError("Canonical onboarding messages are incomplete")
    return {
        "onboarding_message": onboarding_message,
        "discovery_in_progress_message": discovery_message,
    }


@lru_cache(maxsize=2)
def load_output_schema_constraints(path: str = str(UNIVERSAL_LAYER_PATH)) -> dict[str, list[str]]:
    markdown = _read_text(Path(path))
    section = _extract_named_section(markdown, "Required-Keys Tuple (machine readable)", level=3)
    parsed = json.loads(_extract_fenced_block(section, "json"))
    required = parsed.get("required")
    tree_root_types = parsed.get("tree_root_types")
    confidence_values = parsed.get("confidence_values")
    if not isinstance(required, list) or not isinstance(tree_root_types, list) or not isinstance(confidence_values, list):
        raise ValueError("Output schema constraints are malformed")
    return {
        "required": [str(item) for item in required],
        "tree_root_types": [str(item) for item in tree_root_types],
        "confidence_values": [str(item) for item in confidence_values],
    }


@lru_cache(maxsize=2)
def load_worker_prompt_sections(path: str = str(WORKER_PROMPT_SPEC)) -> dict[str, str]:
    markdown = _read_text(Path(path))
    block_7 = _extract_named_section(markdown, "Block 7 — Cache short-circuit (if applicable)", level=3)
    block_8 = _extract_named_section(markdown, "Block 8 — Reconsult Context (Tier 1, if reconsult)", level=3)
    reconsult_template = _extract_fenced_block(block_8, "text")
    block_7_match = re.search(r'"([^"]*cached_bts\[<hash>\][^"]*)"', block_7)
    if not block_7_match:
        raise ValueError("Cache short-circuit template missing from worker prompt spec")
    cache_steering_template = block_7_match.group(1).strip()
    user_message_templates = json.loads(
        _extract_fenced_block(
            _extract_named_section(markdown, "Worker User Message Templates (machine readable)", level=3),
            "json",
        )
    )
    if not isinstance(user_message_templates, dict):
        raise ValueError("Worker user message templates are malformed")
    sections = {
        "cache_steering_template": cache_steering_template,
        "reconsult_template": reconsult_template,
    }
    for key, value in user_message_templates.items():
        sections[f"user_message_{key}"] = str(value)
    return sections


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


def _json_compact(value: Any) -> str:
    return json.dumps(value, separators=(",", ":"), ensure_ascii=True)


def _mask_paths(value: Any, paths: set[str], prefix: str = "") -> Any:
    if not paths:
        return copy.deepcopy(value)
    if isinstance(value, dict):
        masked: dict[str, Any] = {}
        for key, child in value.items():
            child_path = f"{prefix}/{key}" if prefix else key
            if child_path in paths:
                continue
            masked[key] = _mask_paths(child, paths, child_path)
        return masked
    if isinstance(value, list):
        masked_list: list[Any] = []
        for index, child in enumerate(value):
            child_path = f"{prefix}/{index}" if prefix else str(index)
            if child_path in paths:
                continue
            masked_list.append(_mask_paths(child, paths, child_path))
        return masked_list
    return copy.deepcopy(value)


def _collect_override_paths(provisional_data: dict | None) -> set[str]:
    override_paths: set[str] = set()
    for entry in iter_provisional_entries(provisional_data):
        if entry.get("failed_validation_at"):
            continue
        for path in (entry.get("amendments") or {}).get("deprecated_canonical_paths") or []:
            cleaned = str(path or "").strip("/")
            if cleaned:
                override_paths.add(cleaned)
    return override_paths


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

    if not blocks:
        return f"Platform: {display_name} — first encounter, no learned patterns yet."
    return f"Platform: {display_name}\n" + "\n\n".join(blocks)


def format_provisional(provisional_data: dict | None, override_paths: set[str] | None = None) -> str:
    if not provisional_data:
        return ""
    compact = _drop_provenance(copy.deepcopy(provisional_data))
    blocks: list[str] = []
    root_block = format_platform_knowledge(
        {
            key: value
            for key, value in compact.items()
            if key not in {"_recovery_entries", "_extraction_hints"}
        }
    )
    if root_block:
        blocks.append(root_block)
    recovery_entries = []
    for entry in iter_provisional_entries(compact):
        if entry.get("failed_validation_at"):
            continue
        amendments = entry.get("amendments") or {}
        heading = f"entry_id={entry.get('entry_id') or entry.get('request_id') or 'recovery'}"
        if override_paths:
            active = sorted(
                path for path in (amendments.get("deprecated_canonical_paths") or []) if path.strip("/") in override_paths
            )
            if active:
                heading += f" overrides={active}"
        recovery_entries.append(f"{heading}\n{_json_block(amendments)}")
    if recovery_entries:
        blocks.append("Recovery amendments:\n" + "\n\n".join(recovery_entries))
    if not blocks:
        return ""
    return "PROVISIONAL — empirical validation pending:\n" + "\n\n".join(blocks)


def render_knowledge_for_worker(knowledge: dict, provisional: dict | None) -> tuple[str, str]:
    override_paths = _collect_override_paths(provisional)
    masked = _mask_paths(knowledge, override_paths)
    return format_platform_knowledge(masked), format_provisional(provisional, override_paths=override_paths)


def format_reconsult_context(last_result: dict | None, tier: int) -> str:
    if not last_result:
        return ""
    failed_bt = last_result.get("failed_bt")
    bt_debug_tail = last_result.get("bt_debug_tail")
    user_response = last_result.get("user_response")
    sections = load_worker_prompt_sections()
    rendered = sections["reconsult_template"].replace("{TIER}", str(tier))
    rendered = rendered.replace(
        "{FAILED_BT}",
        (
            "Failed BT (the one that did not advance the screen):\n"
            + _json_block(failed_bt)
            + "\n\n"
        )
        if failed_bt
        else "",
    )
    rendered = rendered.replace(
        "{BT_DEBUG_TAIL}",
        ("BT execution log tail:\n" + str(bt_debug_tail).strip() + "\n\n")
        if bt_debug_tail
        else "",
    )
    rendered = rendered.replace(
        "{USER_GUIDANCE}",
        ("User guidance:\n" + str(user_response).strip() + "\n\n")
        if user_response
        else "",
    )
    rendered = re.sub(r"\n{3,}", "\n\n", rendered)
    return rendered.strip()


def format_cache_steering(entry: dict | None, skeleton_hash: str | None) -> str:
    if not isinstance(entry, dict) or str(entry.get("cache_class") or "") != "PROCEDURAL_TEMPLATE":
        return ""
    sections = load_worker_prompt_sections()
    instruction = sections["cache_steering_template"].replace("<hash>", str(skeleton_hash or "unknown"))
    payload = {
        "screen_type": entry.get("screen_type"),
        "cache_class": entry.get("cache_class"),
        "bt": entry.get("bt"),
    }
    return instruction + "\n\nCached structural template:\n" + _json_block(payload)


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


def _node_score(node: Any) -> int:
    if not isinstance(node, dict):
        return 0
    score = 0
    role = node.get("role")
    if role in {"AXLink", "AXButton", "AXHeading", "AXStaticText", "AXProgressIndicator"}:
        score += 10
    for key in ("name", "title", "description", "value"):
        value = node.get(key)
        if isinstance(value, str) and value.strip():
            score += min(len(value.strip()), 40)
    children = node.get("children", [])
    if isinstance(children, list):
        score += min(sum(_node_score(child) for child in children[:8]), 200)
    return score


def prune_ax_tree(tree: dict, target_chars: int = HARD_TREE_CHAR_LIMIT) -> dict:
    web_area = _find_web_area(tree)
    working = web_area if web_area is not None else tree
    pruned = _prune_node(working) or {}
    raw = _json_compact(pruned)
    if len(raw) <= target_chars:
        return pruned

    def shrink(
        node: Any,
        *,
        child_cap: int,
        drop_description_depth: int,
        drop_value_depth: int,
        drop_name_depth: int,
    ) -> Any:
        if not isinstance(node, dict):
            return node
        compact = {
            key: value
            for key, value in node.items()
            if key != "children"
        }
        depth = int(node.get("_depth", 0))
        filtered: dict[str, Any] = {}
        for key, value in compact.items():
            if key == "description" and depth >= drop_description_depth:
                continue
            if key == "value" and depth >= drop_value_depth:
                continue
            if key in {"name", "title"} and depth >= drop_name_depth:
                continue
            filtered[key] = value
        raw_children = list(node.get("children", []))
        if len(raw_children) > child_cap:
            edge_keep = min(max(child_cap // 4, 1), len(raw_children))
            keep_indexes = set(range(edge_keep))
            keep_indexes.update(range(max(len(raw_children) - edge_keep, 0), len(raw_children)))
            remaining_slots = max(child_cap - len(keep_indexes), 0)
            ranked = sorted(
                (
                    (index, child)
                    for index, child in enumerate(raw_children)
                    if index not in keep_indexes
                ),
                key=lambda pair: _node_score(pair[1]),
                reverse=True,
            )[:remaining_slots]
            keep_indexes.update(index for index, _ in ranked)
            raw_children = [child for index, child in enumerate(raw_children) if index in keep_indexes]
        children = []
        for child in raw_children:
            if isinstance(child, dict):
                child = dict(child)
                child["_depth"] = depth + 1
            shrunk = shrink(
                child,
                child_cap=child_cap,
                drop_description_depth=drop_description_depth,
                drop_value_depth=drop_value_depth,
                drop_name_depth=drop_name_depth,
            )
            children.append(shrunk)
        children = [child for child in children if child]
        if children:
            filtered["children"] = children
        filtered.pop("_depth", None)
        return filtered

    attempts = [
        (20, 0, 0, 11),
        (10, 0, 0, 9),
        (5, 0, 0, 7),
        (3, 0, 0, 5),
        (1, 0, 0, 3),
        (1, 0, 0, 1),
    ]
    best = pruned
    best_size = len(raw)
    for child_cap, drop_description_depth, drop_value_depth, drop_name_depth in attempts:
        seeded = dict(pruned)
        seeded["_depth"] = 0
        candidate = shrink(
            seeded,
            child_cap=child_cap,
            drop_description_depth=drop_description_depth,
            drop_value_depth=drop_value_depth,
            drop_name_depth=drop_name_depth,
        ) or {}
        candidate["_notes"] = f"AX_TREE_PRUNED_FROM_{len(raw)}"
        candidate_text = _json_compact(candidate)
        candidate_size = len(candidate_text)
        logger.warning(
            "prompt_codex: iterative AX prune child_cap=%d name_depth=%d size=%d",
            child_cap,
            drop_name_depth,
            candidate_size,
        )
        if candidate_size < best_size:
            best = candidate
            best_size = candidate_size
        if candidate_size <= target_chars:
            return candidate

    if best_size > HARD_TREE_CHAR_LIMIT:
        raise AXTreeTooLargeError(f"ax_tree_too_large_after_prune size={best_size}")
    return best


def _exercise_like_context(screen_context: dict) -> bool:
    marker = str(screen_context.get("screen_type_hint") or "").upper()
    if marker.startswith("EXERCISE"):
        return True
    tree_blob = json.dumps(screen_context.get("tree", {}), ensure_ascii=True)
    for token in ('"AXRadioButton"', '"AXCheckBox"', '"AXTextField"', '"AXComboBox"'):
        if token in tree_blob:
            return True
    return False


def _extract_text_index(tree: dict, max_chars: int = 30000) -> str:
    lines: list[str] = []
    total = 0
    stack = [tree]
    while stack and total < max_chars:
        node = stack.pop(0)
        if not isinstance(node, dict):
            continue
        role = str(node.get("role") or "")
        text = ""
        for key in ("name", "title", "description", "value"):
            value = node.get(key)
            if isinstance(value, str) and value.strip():
                text = value.strip()
                break
        if text and role in {"AXLink", "AXButton", "AXHeading", "AXStaticText"}:
            line = f"{role}: {text}"
            if total + len(line) + 1 > max_chars:
                break
            lines.append(line)
            total += len(line) + 1
        stack.extend(node.get("children", []))
    return "\n".join(lines)


def assemble_system_prompt(
    *,
    universal_sections: dict[str, str],
    platform_data: dict,
    provisional_data: dict | None,
    last_result: dict | None,
    tier: int,
    cache_steering_entry: dict | None = None,
    cache_steering_hash: str | None = None,
) -> str:
    platform_block, provisional_block = render_knowledge_for_worker(platform_data, provisional_data)
    blocks = [
        ("identity", universal_sections["identity"]),
        ("principles", universal_sections["principles"]),
        ("handlers", universal_sections["handlers"]),
        ("output_schema", universal_sections["output_schema"]),
        ("platform_knowledge", platform_block),
    ]
    if provisional_block:
        blocks.append(("provisional", provisional_block))
    cache_steering_block = format_cache_steering(cache_steering_entry, cache_steering_hash)
    if cache_steering_block:
        blocks.append(("cache_steering", cache_steering_block))
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
    screenshot_present: bool,
    relevant_kb_chunks: list[dict] | None,
    screen_context: dict,
) -> str:
    worker_sections = load_worker_prompt_sections()
    pruned_tree = prune_ax_tree(tree)
    tree_text = _json_compact(pruned_tree)
    text_index = _extract_text_index(tree)

    header = (
        worker_sections["user_message_header"]
        .replace("{PLATFORM_DISPLAY_NAME}", platform_display_name)
        .replace("{CURRENT_URL}", current_url or "unavailable")
        .replace("{LAST_SCREEN_TYPE}", last_screen_type or "none")
        .replace("{TIER}", str(tier if tier else "fresh"))
        .replace("{COURSE_ID}", course_id or "unknown")
    )

    sections = [
        header,
        worker_sections["user_message_ax_tree"].replace("{AX_TREE_JSON}", tree_text),
        worker_sections["user_message_text_index"].replace("{TEXT_INDEX}", text_index or "none"),
        worker_sections["user_message_screenshot_present"]
        if screenshot_present
        else worker_sections["user_message_screenshot_absent"],
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
            sections.append(
                worker_sections["user_message_relevant_kb_chunks"].replace(
                    "{KB_CHUNKS}",
                    "\n".join(chunk_lines),
                )
            )

    sections.append(worker_sections["user_message_closing_directive"])

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
