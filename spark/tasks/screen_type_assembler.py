from __future__ import annotations

import json
import logging
import re
import shutil
import tempfile
from pathlib import Path
from typing import Optional

import yaml

from spark.tasks.screen_session import render_for_prompt
from spark.tasks.screen_type_util import MASTER_CATEGORIES, get_master_category

logger = logging.getLogger(__name__)

MAX_TOTAL_PROMPT_CHARS = 25_000
UNIVERSAL_CHAR_BUDGET = 5_000
KB_CHAR_BUDGET = 5_000
HANDOFF_ROOT = Path("/tmp/taey-ed-worker-handoff")
SPLIT_MASTER_CATEGORIES = {"NAVIGATION", "ARTICLE", "VIDEO", "TRANSITION"}

KNOWN_ACTIONS = {
    "click",
    "click_at",
    "conditional",
    "discover_menu",
    "drag",
    "extract_question",
    "fallback",
    "find_all",
    "find_and_click",
    "find_and_type",
    "for_each",
    "lookup_match",
    "press_escape",
    "press_key",
    "scroll",
    "select_dropdown_option",
    "send_to_llm",
    "solve_assessment_page",
    "store_qa",
    "type_keys",
    "video_poll",
    "wait",
    "wait_for_element",
}
SIGNATURE_ACTIONS = {
    "conditional",
    "discover_menu",
    "drag",
    "extract_question",
    "fallback",
    "find_all",
    "find_and_type",
    "for_each",
    "lookup_match",
    "press_key",
    "scroll",
    "select_dropdown_option",
    "send_to_llm",
    "store_qa",
    "video_poll",
}
FLAT_DRAG_KEYS = {"start_x", "start_y", "from_x", "from_y", "to_x", "to_y", "end_x", "end_y"}
TREE_VALUE_CHAR_LIMIT = 2_000
_ALLOWED_TREE_KEYS = {
    "ChromeAXNodeId",
    "aRIACurrent",
    "aRIAPosInSet",
    "aRIASetSize",
    "activationPoint",
    "autocompleteValue",
    "blockQuoteLevel",
    "children",
    "childrenInNavigationOrder",
    "description",
    "disclosureLevel",
    "document",
    "edited",
    "elementBusy",
    "enabled",
    "expanded",
    "focused",
    "frame",
    "fullScreen",
    "hasPopup",
    "help",
    "insertionPointLineNumber",
    "invalid",
    "keyShortcutsValue",
    "language",
    "linkedUIElements",
    "loaded",
    "loadingProgress",
    "main",
    "maxValue",
    "minValue",
    "minimized",
    "modal",
    "name",
    "numberOfCharacters",
    "placeholderValue",
    "popupValue",
    "position",
    "required",
    "role",
    "roleDescription",
    "rows",
    "sections",
    "selected",
    "selectedChildren",
    "selectedRows",
    "selectedText",
    "selectedTextRange",
    "selectedTextRanges",
    "size",
    "startTextMarker",
    "subrole",
    "title",
    "uRL",
    "value",
    "valueDescription",
    "visibleCharacterRange",
    "visited",
}


class ScreenTypeAssemblerError(RuntimeError):
    """Raised when prompt assembly or recipe conformance fails."""


def _platforms_dir() -> Path:
    candidates = [
        Path(__file__).parent.parent / "platforms",
        Path("spark/platforms"),
    ]
    for path in candidates:
        if path.exists():
            return path
    return candidates[0]


def _screen_types_dir(platform: str) -> Path:
    return _platforms_dir() / platform / "screen_types"


def _load_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except OSError as e:
        raise ScreenTypeAssemblerError(f"Failed to read {path}: {e}") from e


def _extract_top_level_value(text: str, key: str) -> str:
    prefix = f"{key}:"
    for line in text.splitlines():
        if line.startswith(prefix):
            return line.split(":", 1)[1].strip().split("#", 1)[0].strip()
    return ""


def _extract_top_level_block(text: str, key: str) -> str:
    lines = text.splitlines()
    prefix = f"{key}:"
    block: list[str] = []
    in_block = False
    for line in lines:
        if not in_block:
            if line.startswith(prefix):
                in_block = True
                block.append(line)
                continue
        else:
            if line and not line.startswith((" ", "\t", "#")) and ":" in line:
                break
            block.append(line)
    return "\n".join(block)


def _load_screen_artifact(platform: str, screen_type: str) -> dict:
    normalized = str(screen_type or "UNKNOWN").strip() or "UNKNOWN"
    screen_dir = _screen_types_dir(platform)
    if normalized == "UNKNOWN":
        path = screen_dir / "_UNKNOWN_GUIDE.md"
        if not path.exists():
            raise ScreenTypeAssemblerError(f"UNKNOWN guide missing for {platform}: {path}")
        return {
            "screen_type": "UNKNOWN",
            "kind": "unknown_guide",
            "path": path,
            "content": _load_text(path),
        }

    if normalized.upper() in SPLIT_MASTER_CATEGORIES:
        guide_path = screen_dir / "_UNKNOWN_GUIDE.md"
        if guide_path.exists():
            logger.warning(
                "screen_type_assembler: bare split master screen_type=%s for %s; falling back to UNKNOWN guide",
                normalized,
                platform,
            )
            return {
                "screen_type": "UNKNOWN",
                "kind": "unknown_guide",
                "path": guide_path,
                "content": _load_text(guide_path),
            }
        raise ScreenTypeAssemblerError(
            f"Bare split-master screen_type cannot be assembled: platform={platform!r} screen_type={normalized!r}"
        )

    path = screen_dir / f"{normalized}.yaml"
    if path.exists():
        return {
            "screen_type": normalized,
            "kind": "yaml",
            "path": path,
            "content": _load_text(path),
        }

    guide_path = screen_dir / "_UNKNOWN_GUIDE.md"
    if guide_path.exists():
        logger.warning(
            "screen_type_assembler: unresolved screen_type=%s for %s; falling back to UNKNOWN guide",
            normalized,
            platform,
        )
        return {
            "screen_type": "UNKNOWN",
            "kind": "unknown_guide",
            "path": guide_path,
            "content": _load_text(guide_path),
        }

    raise ScreenTypeAssemblerError(
        f"No per-screen artifact for platform={platform!r} screen_type={normalized!r}"
    )


def load_screen_artifact_metadata(platform: str, screen_type: str) -> dict:
    artifact = _load_screen_artifact(platform, screen_type)
    content = artifact["content"]
    deterministic_raw = _extract_top_level_value(content, "deterministic").lower()
    deterministic = deterministic_raw == "true"
    fixed_bt = None
    fixed_bt_block = _extract_top_level_block(content, "fixed_behavior_tree")
    if fixed_bt_block:
        try:
            fixed_bt = (yaml.safe_load(fixed_bt_block) or {}).get("fixed_behavior_tree")
        except Exception as e:
            raise ScreenTypeAssemblerError(
                f"Malformed fixed_behavior_tree in {artifact['path']}: {e}"
            ) from e
    return {
        "artifact": artifact,
        "deterministic": deterministic,
        "fixed_behavior_tree": fixed_bt,
    }


def _render_universal_block() -> str:
    universal_path = _platforms_dir() / "_universal.json"
    if not universal_path.exists():
        raise ScreenTypeAssemblerError(f"Universal rules file missing: {universal_path}")
    try:
        payload = json.loads(universal_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        raise ScreenTypeAssemblerError(f"Universal rules malformed: {e}") from e

    lines = [
        "=== UNIVERSAL RULES (always apply) ===",
        "Platform-agnostic operating law. Follow these rules together with the selected screen program.",
        "",
    ]
    cats = payload.get("screen_categories") or {}
    if cats.get("categories"):
        lines.append("SCREEN CATEGORIES (the 6 master types): " + ", ".join(cats["categories"]))
        if cats.get("rule"):
            lines.append(str(cats["rule"]).strip())
        lines.append("")
    for idx, note in enumerate(payload.get("operational_notes") or [], 1):
        rule = str(note.get("rule") or "").strip()
        anti_pattern = str(note.get("anti_pattern") or "").strip()
        scope = str(note.get("scope") or "").strip()
        lines.append(f"{idx}. {rule}")
        if scope:
            lines.append(f"   scope: {scope}")
        if anti_pattern:
            lines.append(f"   anti-pattern: {anti_pattern}")
    rendered = "\n".join(lines).strip()
    if len(rendered) > UNIVERSAL_CHAR_BUDGET:
        raise ScreenTypeAssemblerError(
            f"Universal rules exceed budget: {len(rendered)} > {UNIVERSAL_CHAR_BUDGET}"
        )
    return rendered


def _render_session_block(platform: str, tree: dict) -> str:
    from spark.tasks.skeleton import extract_content_fingerprint, extract_skeleton, skeleton_hash

    return render_for_prompt(
        platform=platform,
        skel_hash=skeleton_hash(extract_skeleton(tree)),
        fingerprint=extract_content_fingerprint(tree),
    )


def _render_kb_block(screen_type: str, kb_chunks: list[dict]) -> str:
    master = get_master_category(screen_type)
    if master != "EXERCISE":
        return ""

    lines = [
        "=== RELEVANT COURSE CONTEXT ===",
        "Use only the included local-KB chunks below. They were preselected on the user's machine.",
        "",
    ]
    total = len("\n".join(lines))
    included = 0
    for idx, chunk in enumerate(kb_chunks or [], 1):
        text = str(chunk.get("text") or "").strip()
        if not text:
            continue
        src_type = str(chunk.get("source_screen_type") or "?")
        score = chunk.get("score")
        header = f"--- chunk {idx} [{src_type}]"
        if isinstance(score, float):
            header += f" score={score:.3f}"
        header += " ---"
        entry = "\n".join([header, text, ""])
        if total + len(entry) > KB_CHAR_BUDGET:
            break
        lines.append(header)
        lines.append(text)
        lines.append("")
        total += len(entry)
        included += 1
    if included == 0:
        return ""
    return "\n".join(lines).strip()


def _render_proven_knowledge_block(platform: str, screen_type: str) -> str:
    """Inject the accumulated, verified per-subtype know-how the worker needs
    to rebuild a correct adaptive BT.

    Restored 2026-06-13: the subtype cutover removed this injection, starving
    the worker of the proven procedure (e.g. the verified×17 dropdown pattern:
    open each AXComboBox -> discover_menu enumerates options -> send_to_llm
    matches content -> select_dropdown_option per row -> Check). Without it the
    worker shortcut the recipe, omitting required phases, and conformance
    rejected every build. These notes live at
    {DATA_DIR}/knowledge_notes/{platform}/{MASTER}.{subtype}.md and carry
    verified×N markers so the worker weights proven patterns over synthesized.
    """
    from spark.tasks.paths import DATA_DIR

    master = get_master_category(screen_type)
    if not master or "_" not in screen_type:
        return ""
    subtype = screen_type[len(master) + 1:].lower()
    if not subtype:
        return ""
    note_path = DATA_DIR / "knowledge_notes" / platform / f"{master}.{subtype}.md"
    if not note_path.exists():
        return ""
    try:
        content = note_path.read_text(encoding="utf-8").strip()
    except OSError:
        return ""
    if not content:
        return ""
    return (
        "=== PROVEN KNOWLEDGE FOR THIS SCREEN TYPE (verified observations — FOLLOW THESE) ===\n"
        "Accumulated, production-verified procedure for this subtype. Higher verified×N = "
        "more trustworthy; prefer it over improvisation. Build the BT that satisfies the "
        "recipe's required phases using exactly these patterns.\n\n"
        + content
    )


def assemble_worker_prompt(
    *,
    tree: dict,
    platform: str,
    consultation_id: str,
    screen_type: str,
    kb_chunks: Optional[list[dict]] = None,
) -> tuple[str, dict]:
    artifact = _load_screen_artifact(platform, screen_type)
    sections = [
        f"=== WORKER CONTEXT ===\nconsultation_id: {consultation_id}\nplatform: {platform}\nscreen_type_hint: {screen_type}",
        _render_universal_block(),
        f"=== SCREEN PROGRAM ===\nsource: {artifact['path']}\nkind: {artifact['kind']}\n\n{artifact['content'].rstrip()}",
    ]
    # NOTE (2026-06-13): the concrete behavior_tree_template now lives in the
    # per-screen recipe YAML and supersedes the verbose knowledge-note dump that
    # used to be injected here. Injecting both blew the 25K prompt cap
    # (29960 > 25000 -> assembly failure -> fallback -> escalation loop). The
    # template + recipe carry the procedure; keep the prompt lean.
    session_block = _render_session_block(platform, tree)
    if session_block:
        sections.append(session_block)
    kb_block = _render_kb_block(screen_type, kb_chunks or [])
    if kb_block:
        sections.append(kb_block)

    prompt = "\n\n".join(section.strip() for section in sections if section.strip())
    if len(prompt) >= MAX_TOTAL_PROMPT_CHARS:
        raise ScreenTypeAssemblerError(
            f"assembled prompt exceeds hard cap after KB injection: {len(prompt)} >= {MAX_TOTAL_PROMPT_CHARS}"
        )
    return prompt, {
        "artifact_path": str(artifact["path"]),
        "artifact_kind": artifact["kind"],
        "artifact_screen_type": artifact["screen_type"],
        "prompt_chars": len(prompt),
        "kb_chars": len(kb_block),
        "kb_chunks_included": kb_block.count("--- chunk "),
    }


def _sanitize_tree_for_worker(tree: dict) -> dict:
    def _sanitize_value(value):
        if isinstance(value, dict):
            sanitized = {}
            for key, child in value.items():
                if key not in _ALLOWED_TREE_KEYS:
                    continue
                sanitized[key] = _sanitize_value(child)
            return sanitized
        if isinstance(value, list):
            sanitized_items = [_sanitize_value(item) for item in value]
            return [item for item in sanitized_items if item not in (None, {}, [])]
        if isinstance(value, str):
            if len(value) > TREE_VALUE_CHAR_LIMIT:
                return value[:TREE_VALUE_CHAR_LIMIT] + "…[truncated]"
            return value
        return value

    sanitized = _sanitize_value(tree)
    if not isinstance(sanitized, dict):
        raise ScreenTypeAssemblerError("sanitized tree must remain a dict")
    return sanitized


def create_worker_handoff(
    *,
    tree: dict,
    platform: str,
    consultation_id: str,
    screen_type: str,
    screenshot_path: Path,
    kb_chunks: Optional[list[dict]] = None,
) -> tuple[Path, dict]:
    system_prompt, prompt_meta = assemble_worker_prompt(
        tree=tree,
        platform=platform,
        consultation_id=consultation_id,
        screen_type=screen_type,
        kb_chunks=kb_chunks,
    )
    if not screenshot_path.exists():
        raise ScreenTypeAssemblerError(f"Screenshot missing for handoff: {screenshot_path}")
    HANDOFF_ROOT.mkdir(parents=True, exist_ok=True)
    handoff_dir = Path(
        tempfile.mkdtemp(prefix=f"{consultation_id}_", dir=str(HANDOFF_ROOT))
    )
    tree_path = handoff_dir / "tree.json"
    screenshot_target = handoff_dir / screenshot_path.name
    system_prompt_path = handoff_dir / "system_prompt.txt"
    try:
        # Base Chrome filter (Jesse 2026-06-13): scope to page content + strip
        # browser chrome for EVERY worker tree — the worker was getting the full
        # ~1.4MB tree including Chrome toolbar/tabs/extensions, pure bloat and
        # confusion. filter_tree_base = web-area scope + content-keep + collapse
        # (no value truncation). Per-screen YAMLs narrow further.
        from spark.tasks.prune_tree import filter_tree_base
        tree_path.write_text(
            json.dumps(filter_tree_base(tree), ensure_ascii=True, indent=2),
            encoding="utf-8",
        )
        system_prompt_path.write_text(system_prompt, encoding="utf-8")
        shutil.copy2(screenshot_path, screenshot_target)
    except OSError as e:
        raise ScreenTypeAssemblerError(f"Failed to stage worker handoff: {e}") from e
    handoff_meta = {
        **prompt_meta,
        "handoff_dir": str(handoff_dir),
        "tree_path": str(tree_path),
        "screenshot_path": str(screenshot_target),
        "system_prompt_path": str(system_prompt_path),
    }
    return handoff_dir, handoff_meta


def _split_top_level_sections(text: str) -> dict[str, str]:
    sections: dict[str, list[str]] = {}
    current: Optional[str] = None
    for line in text.splitlines():
        match = re.match(r"^([a-z_]+):(?:\s|$)", line)
        if match:
            current = match.group(1)
            sections.setdefault(current, []).append(line)
            continue
        if current is not None:
            sections[current].append(line)
    return {key: "\n".join(lines).strip() for key, lines in sections.items()}


def _collect_tree_actions(node: dict) -> set[str]:
    found: set[str] = set()
    node_type = node.get("type", "action")
    if node_type == "fallback":
        found.add("fallback")
    action_name = node.get("action")
    if isinstance(action_name, str):
        found.add(action_name)
    for child in node.get("children") or []:
        if isinstance(child, dict):
            found.update(_collect_tree_actions(child))
    for key in ("do", "then", "else"):
        child = node.get(key)
        if isinstance(child, dict):
            found.update(_collect_tree_actions(child))
    return found


def _iter_nodes(node: dict):
    yield node
    for child in node.get("children") or []:
        if isinstance(child, dict):
            yield from _iter_nodes(child)
    for key in ("do", "then", "else"):
        child = node.get(key)
        if isinstance(child, dict):
            yield from _iter_nodes(child)


def _strip_yaml_comments(text: str) -> str:
    """Remove YAML inline/full-line comments (# at line start or after
    whitespace) so prose in COMMENTS never contributes a 'required' action.

    Operator defect 2026-06-14: _recipe_actions scanned the whole recipe text for
    action tokens and couldn't tell prohibition from prescription — 'scroll'
    mentioned only in a 'NO scroll phase' comment became a REQUIRED signature
    action the worker correctly never emitted. Same class as the find_all-in-a-
    comment footgun. Comments are guidance, never requirements."""
    out = []
    for line in text.splitlines():
        m = re.search(r"(^|\s)#", line)
        out.append(line[: m.start()] if m else line)
    return "\n".join(out)


def _recipe_actions(recipe_text: str) -> set[str]:
    scanned = _strip_yaml_comments(recipe_text)
    found = set()
    for action in KNOWN_ACTIONS:
        if re.search(rf"(?<![A-Za-z_]){re.escape(action)}(?![A-Za-z_])", scanned):
            found.add(action)
    return found


def validate_worker_bt_response(parsed: dict, platform: str, screen_type: str) -> None:
    artifact = _load_screen_artifact(platform, screen_type)

    session = parsed.get("_session")
    if session is not None:
        if not isinstance(session, dict):
            raise ScreenTypeAssemblerError("_session must be an object when present")
        facts = session.get("facts")
        if facts is not None and not isinstance(facts, dict):
            raise ScreenTypeAssemblerError("_session.facts must be an object when present")

    if artifact["kind"] != "yaml":
        emitted = str(parsed.get("screen_type") or "").strip().upper()
        if emitted and emitted not in MASTER_CATEGORIES and not any(
            emitted.startswith(master + "_") for master in MASTER_CATEGORIES
        ):
            raise ScreenTypeAssemblerError(f"UNKNOWN-guide response emitted invalid screen_type {emitted!r}")
        return

    emitted_screen_type = str(parsed.get("screen_type") or "").strip()
    if emitted_screen_type != artifact["screen_type"]:
        raise ScreenTypeAssemblerError(
            f"worker changed screen_type from {artifact['screen_type']!r} to {emitted_screen_type!r}"
        )

    sections = _split_top_level_sections(str(artifact["content"]))
    recipe_text = sections.get("recipe", "")
    allowed_actions = _recipe_actions(recipe_text)
    actual_actions = _collect_tree_actions(parsed["tree"])

    disallowed = sorted(actual_actions - allowed_actions)
    if disallowed:
        raise ScreenTypeAssemblerError(
            f"worker emitted actions not present in the canonical recipe: {', '.join(disallowed)}"
        )

    required = sorted((allowed_actions & SIGNATURE_ACTIONS) - {"conditional", "fallback"})
    missing = [action for action in required if action not in actual_actions]
    if missing:
        raise ScreenTypeAssemblerError(
            f"worker omitted required recipe phases/actions: {', '.join(missing)}"
        )

    for node in _iter_nodes(parsed["tree"]):
        if node.get("type") == "fallback" and "fallback" not in allowed_actions:
            raise ScreenTypeAssemblerError("fallback node not allowed by this screen recipe")
        action_name = node.get("action")
        params = node.get("params")
        if not isinstance(params, dict):
            continue
        if action_name == "ax_press":
            raise ScreenTypeAssemblerError("ax_press is forbidden in worker BTs")
        if params.get("match_mode") == "contains":
            raise ScreenTypeAssemblerError("match_mode contains is forbidden")
        if params.get("strategy") == "focus_space":
            raise ScreenTypeAssemblerError("focus_space strategy is forbidden")
        if action_name == "drag" and any(key in params for key in FLAT_DRAG_KEYS):
            raise ScreenTypeAssemblerError("flat drag keys are forbidden; use nested start/end objects")
