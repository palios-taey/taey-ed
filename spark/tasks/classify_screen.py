"""
Screen classification helpers.

Post-scr1 slash shape:
  - classify_screen() answers "what screen is this?"
  - build_extract_config() generates content-extraction criteria
  - no server-side BT builder remains here
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Optional

from spark.tasks.claude_runner import ClaudeCallError, call_claude_cli
from spark.tasks.prompt_codex import _count_roles, _find_web_area, analyze_tree
from spark.tasks.prune_tree import prune_tree_for_prompt
from spark.tasks.screen_type_util import get_master_category

logger = logging.getLogger(__name__)

SCREEN_CATEGORIES = {
    "NAVIGATION": "Content list, module overview, course home, dashboard. Primary action: pick which content to go to next.",
    "VIDEO": "Video content delivery. May be unstarted, playing, or complete.",
    "ARTICLE": "Reading/text content page.",
    "EXERCISE": "Interactive assessment.",
    "TRANSITION": "Click-through screen: score card, start, continue, completion modal.",
    "UNKNOWN": "Does not fit any category above. Requires human review.",
}
SPLIT_MASTER_CATEGORIES = {"NAVIGATION", "ARTICLE", "VIDEO", "TRANSITION"}
LEGACY_VARIANT_MAP = {
    "ARTICLE": "ARTICLE__READING",
    "ARTICLE_READING": "ARTICLE__READING",
    "ARTICLE_COMPLETE": "ARTICLE__COMPLETE",
    "VIDEO": "VIDEO__PLAYER",
    "VIDEO_PLAYER": "VIDEO__PLAYER",
    "VIDEO_UNSTARTED": "VIDEO__PLAYER",
    "VIDEO_PLAYING": "VIDEO__PLAYER",
    "VIDEO_COMPLETE": "VIDEO__PLAYER",
    "NAVIGATION": "NAVIGATION__COURSE_DASHBOARD",
    "NAVIGATION_COURSE_OVERVIEW": "NAVIGATION__COURSE_DASHBOARD",
    "NAVIGATION_UNIT_OVERVIEW": "NAVIGATION__UNIT_OVERVIEW",
    "NAVIGATION_LESSON_LIST": "NAVIGATION__LESSON_LIST",
    "TRANSITION": "TRANSITION__SUMMARY",
    "TRANSITION_INTRO": "TRANSITION__INTRO",
    "TRANSITION_SUMMARY": "TRANSITION__SUMMARY",
    "TRANSITION_PRACTICE_INTRO": "TRANSITION__INTRO",
    "TRANSITION_EXERCISE_NEXT": "TRANSITION__SUMMARY",
}

CLASSIFICATION_PROMPT = """\
You are classifying a screen from an educational platform (LMS).

Look at the screenshot and accessibility tree below. Determine the canonical
screen_type for this screen.

=== PLATFORM ===
{platform}

{platform_context}

=== ALLOWED SCREEN TYPES ===
{allowed_screen_types}

=== ACCESSIBILITY TREE ===
{tree_json}

=== INSTRUCTIONS ===
1. Look at the screenshot first — what is the primary content on this page?
2. Check the accessibility tree for confirming signals (roles, button names, etc.)
3. Focus on the MAIN CONTENT AREA, not sidebars or browser chrome.
4. Return EXACTLY one value from the allowed screen types list, or UNKNOWN.
5. Never return a bare master category like NAVIGATION, ARTICLE, VIDEO, or TRANSITION.
6. If you're not sure, return UNKNOWN. Do not guess.

=== RESPONSE FORMAT ===
Return ONLY valid JSON, no markdown, no explanation outside the JSON:
{{
  "screen_type": "One exact allowed screen type or UNKNOWN",
  "confidence_note": "Brief 1-sentence explanation of why this classification",
  "platform_variant": "Repeat the same exact screen_type when known, or empty string"
}}
"""

_EXTRACT_PROMPT = """\
You are analyzing an educational platform screen to determine what UNIQUE CONTENT
should be extracted for the student's learning records.

PLATFORM: {platform}
SCREEN TYPE: {screen_type}

=== WHAT TO EXTRACT ===
Capture the educational content that a student would want to review later.
NOT page chrome, sidebars, navigation menus, or UI labels.

=== ACCESSIBILITY TREE ===
{tree_json}

=== YOUR TASK ===
1. Find the MAIN CONTENT AREA.
2. Identify the parent container that holds the unique content.
3. Build an extraction config that targets ONLY that content area.

=== EXTRACTION CONFIG FORMAT ===
Return ONLY valid JSON:
{{
  "text": [
    {{
      "role": "AXStaticText",
      "parent_contains": "<name of the content container you found>"
    }}
  ],
  "images": [
    {{
      "source": "window",
      "purpose": "<what educational content to describe>"
    }}
  ]
}}

Return null if there is genuinely no content to extract.
"""

_NO_EXTRACT_TYPES = {"NAVIGATION", "TRANSITION"}


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
    return path.read_text(encoding="utf-8")


def _extract_top_level_value(text: str, key: str) -> str:
    prefix = f"{key}:"
    for line in text.splitlines():
        if line.startswith(prefix):
            return line.split(":", 1)[1].strip()
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
                remainder = line.split(":", 1)[1].strip()
                if remainder and remainder not in {">", "|"}:
                    block.append(remainder)
                continue
        else:
            if line and not line.startswith((" ", "\t", "#")) and ":" in line:
                break
            if line.strip().startswith("#"):
                continue
            block.append(line.strip())
    return " ".join(part for part in block if part).strip()


def _load_screen_type_registry(platform: str) -> dict[str, dict]:
    registry: dict[str, dict] = {}
    for path in sorted(_screen_types_dir(platform).glob("*.yaml")):
        text = _load_text(path)
        screen_type = _extract_top_level_value(text, "screen_type") or path.stem
        if screen_type in SPLIT_MASTER_CATEGORIES:
            continue
        if "__" not in screen_type and not screen_type.startswith("EXERCISE_"):
            continue
        registry[screen_type] = {
            "path": path,
            "classify": _extract_top_level_block(text, "classify"),
        }
    return registry


def _render_allowed_screen_types(platform: str) -> str:
    registry = _load_screen_type_registry(platform)
    lines = []
    for screen_type, entry in registry.items():
        classify = entry["classify"] or "No classify hint provided."
        lines.append(f"- {screen_type}: {classify}")
    return "\n".join(lines)


def _flatten_text(tree: dict) -> str:
    parts: list[str] = []
    stack = [tree]
    while stack:
        node = stack.pop()
        if not isinstance(node, dict):
            continue
        for key in ("name", "title", "description", "value"):
            value = str(node.get(key) or "").strip()
            if value:
                parts.append(value)
        stack.extend(reversed(node.get("children", [])))
    return "\n".join(parts).lower()


def _infer_navigation_subtype(tree: dict) -> str:
    text = _flatten_text(_find_web_area(tree))
    if "lesson " in text:
        return "NAVIGATION__LESSON_LIST"
    if "course mastery" in text or "mastery challenge" in text:
        return "NAVIGATION__COURSE_DASHBOARD"
    if "unit " in text:
        return "NAVIGATION__UNIT_OVERVIEW"
    return "UNKNOWN"


def _infer_article_subtype(tree: dict) -> str:
    text = _flatten_text(_find_web_area(tree))
    if "completed article" in text or "completed video" in text:
        return "ARTICLE__COMPLETE"
    if "up next" in text:
        return "ARTICLE__READING"
    return "UNKNOWN"


def _infer_transition_subtype(tree: dict) -> str:
    text = _flatten_text(_find_web_area(tree))
    # SUMMARY-DEFINITIVE markers FIRST. An end-of-set summary carries forward
    # controls ("Keep going" / "Let's go" to the next item) that ALSO match the
    # INTRO tokens — so checking INTRO first mislabels every summary as an intro
    # (live RCA 2026-06-15: a summary with "mastery points"+"up next" matched
    # "keep going" -> TRANSITION__INTRO -> the button-hunting INTRO fixed BT ran
    # on a link-based summary -> 422/find-fail). "mastery points" / "show summary"
    # / "up next" only appear post-completion, so they are the stronger signal.
    if any(token in text for token in ("mastery points", "show summary", "up next", "correct)", "try again")):
        return "TRANSITION__SUMMARY"
    if any(token in text for token in ("let’s go", "let's go", "start quiz", "start unit test", "keep going", "don't give up yet")):
        return "TRANSITION__INTRO"
    return "UNKNOWN"


def _infer_exercise_subtype(tree: dict) -> str:
    """Resolve a bare EXERCISE master to its on-disk subtype from widget shape.

    Completes the canonicalize() subtype-inference pattern that NAVIGATION,
    ARTICLE, VIDEO, and TRANSITION already have. EXERCISE was omitted in the
    subtype cutover (2026-06-12), so a bare/legacy EXERCISE hash entry fell to
    UNKNOWN and the worker received the UNKNOWN classification guide with no
    recipe — it then invented unregistered actions (describe_image) and failed.

    Role-accurate (AXComboBox / AXRadioButton / AXCheckBox), and deterministic
    ONLY for the unambiguous widget signatures (dropdown, single-vs-multi
    choice). Genuinely ambiguous shapes (matcher vs sorter, graph points,
    label-image, table, free text) return UNKNOWN so the Claude classifier
    picks the subtype — never a wrong guess (wrong answer = catastrophic).
    """
    combobox_answer = 0
    choice_radio = 0
    choice_checkbox = 0
    post_answer = False
    wrong_verdict = False
    has_check = False
    has_answer_widget = False

    def walk(n):
        nonlocal combobox_answer, choice_radio, choice_checkbox, post_answer
        nonlocal wrong_verdict, has_check, has_answer_widget
        if isinstance(n, dict):
            role = n.get("role") or ""
            name = (n.get("name") or "").strip().lower()
            value = str(n.get("value") or "").strip().lower()
            if role in ("AXComboBox", "AXTextField", "AXTextArea", "AXCheckBox", "AXRadioButton"):
                has_answer_widget = True
            # WRONG-ANSWER state markers (operator gap #3, 2026-06-15): the answer
            # was submitted and judged WRONG — input intact, a "Not quite yet"/
            # "Try again" verdict, and NO "Check" button (the no-Check signal is
            # the discriminator vs a fresh EXERCISE_*). Routed to EXERCISE_WRONG_
            # ANSWER (escalate-only) so it does NOT mis-classify as a fresh
            # exercise and get re-answered.
            if "not quite" in name or name == "try again" or "try again" in name:
                wrong_verdict = True
            if role == "AXButton" and (name == "check" or name.startswith("check ")):
                has_check = True
            # POST-ANSWER markers: these buttons appear ONLY after an answer is
            # submitted (the correct/incorrect verdict state). Their presence
            # means this is an answered->advance TRANSITION, not a fresh exercise
            # to solve. Live RCA 2026-06-15 (operator): without this, an answered
            # dropdown re-entered classification, fired EXERCISE_DROPDOWN, the
            # worker correctly omitted the solve actions to advance, and
            # conformance rejected it -> stuck before it could advance.
            if role == "AXButton" and ("next question" in name or "step-by-step solution" in name):
                post_answer = True
            # A dropdown is a fresh exercise to SOLVE only while UNANSWERED. The
            # label name ('select an answer') is ALWAYS present; the VALUE is
            # empty/placeholder when unanswered and the chosen option ('4','1')
            # once answered. Gate on the value so an answered dropdown is not
            # misclassified as fresh.
            if role == "AXComboBox" and "select an answer" in name:
                if value in ("", "select an answer"):
                    combobox_answer += 1
            if name.startswith("(choice"):
                if role == "AXCheckBox":
                    choice_checkbox += 1
                elif role == "AXRadioButton":
                    choice_radio += 1
            for c in n.get("children") or []:
                walk(c)
        elif isinstance(n, list):
            for i in n:
                walk(i)

    walk(_find_web_area(tree) or tree)
    # An answered / post-answer-CORRECT screen ("Next question") is not a fresh
    # exercise — return UNKNOWN so the transition classifier routes it to advance.
    if post_answer:
        return "UNKNOWN"
    # POST-WRONG-ANSWER state: input intact + wrong verdict + NO Check button.
    # Distinct screen type so it ESCALATES (its recipe emits {} for a clean ladder
    # escalation) instead of being re-classified as a fresh exercise and
    # re-answered. The no-Check signal discriminates it from a fresh EXERCISE_*.
    if wrong_verdict and has_answer_widget and not has_check:
        return "EXERCISE_WRONG_ANSWER"
    if combobox_answer > 0:
        return "EXERCISE_DROPDOWN"
    if choice_checkbox > 0:
        return "EXERCISE_MULTIPLE_SELECT"
    if choice_radio > 0:
        return "EXERCISE_MULTIPLE_CHOICE"
    return "UNKNOWN"


def canonicalize_screen_type(platform: str, screen_type: object, tree: dict | None = None) -> str:
    registry = _load_screen_type_registry(platform)
    allowed = set(registry)
    normalized = str(screen_type or "UNKNOWN").strip().upper() or "UNKNOWN"
    if normalized in allowed:
        return normalized

    mapped = LEGACY_VARIANT_MAP.get(normalized, normalized)
    if mapped in allowed:
        return mapped

    if tree is not None:
        if normalized in {"NAVIGATION", "NAVIGATION_COURSE_OVERVIEW"}:
            inferred = _infer_navigation_subtype(tree)
            if inferred in allowed:
                return inferred
        if normalized in {"ARTICLE", "ARTICLE__READING", "ARTICLE__COMPLETE"}:
            inferred = _infer_article_subtype(tree)
            if inferred in allowed:
                return inferred
        if normalized in {"VIDEO", "VIDEO_UNSTARTED", "VIDEO_PLAYING", "VIDEO_COMPLETE"}:
            if "VIDEO__PLAYER" in allowed:
                return "VIDEO__PLAYER"
        if normalized.startswith("TRANSITION"):
            inferred = _infer_transition_subtype(tree)
            if inferred in allowed:
                return inferred
        if normalized == "EXERCISE" or normalized.startswith("EXERCISE"):
            inferred = _infer_exercise_subtype(tree)
            if inferred in allowed:
                return inferred

    master = get_master_category(normalized)
    if master in SPLIT_MASTER_CATEGORIES:
        return "UNKNOWN"
    return "UNKNOWN"


def _load_platform_context(platform: str) -> str:
    from spark.tasks.knowledge_loader import load_knowledge

    knowledge = load_knowledge(platform)
    if not knowledge:
        return ""

    parts = []
    registry = _load_screen_type_registry(platform)
    if registry:
        parts.append("=== CANONICAL SCREEN TYPES ===\n" + ", ".join(sorted(registry.keys())))

    quirks = knowledge.get("global", {}).get("platform_quirks", [])
    if quirks:
        quirk_lines = []
        for q in quirks:
            quirk_lines.append(
                f"- {q.get('quirk', '')}: {q.get('workaround', '')} (affects: {', '.join(q.get('affects', []))})"
            )
        parts.append("=== PLATFORM QUIRKS ===\n" + "\n".join(quirk_lines))

    indicators = knowledge.get("accessibility_tree_guide", {}).get("completion_indicators_in_tree", {})
    if indicators:
        parts.append(
            "=== COMPLETION INDICATORS IN TREE ===\n"
            + "\n".join(f"- {k}: {v}" for k, v in indicators.items())
        )
    return "\n\n".join(parts)


def _llm_call(prompt: str, screenshot_b64: Optional[str] = None) -> Optional[str]:
    try:
        raw, _meta = call_claude_cli(
            system_prompt="You classify or analyze educational-platform screens. Reply with ONLY the JSON object the user asks for.",
            user_message=prompt,
            screenshot_b64=screenshot_b64,
            require_screenshot_read=bool(screenshot_b64),
            permission_mode="dontAsk",
            tools=["Read"] if screenshot_b64 else [],
        )
        return raw.strip()
    except ClaudeCallError as e:
        logger.error(f"LLM call failed: {e}")
        return None
    except Exception as e:
        logger.error(f"LLM call unexpected error: {e}")
        return None


def classify_screen(tree: dict, screenshot_b64: Optional[str], platform: str) -> dict:
    raw: str | None = ""
    try:
        from spark.tasks.skeleton import extract_skeleton, skeleton_hash
        from spark.tasks.screen_signatures import match_signature
        from spark.tasks.variant_cache import lookup_by_hash

        allowed_screen_types = _render_allowed_screen_types(platform)
        platform_context = _load_platform_context(platform)
        scoped_tree = _find_web_area(tree)
        skel_hash = skeleton_hash(extract_skeleton(scoped_tree))

        # DETERMINISTIC WIDGET READ WINS FIRST. An unambiguous answer-widget
        # signature (AXComboBox 'select an answer' -> DROPDOWN; '(choice...'
        # radio/checkbox -> single/multi choice) is GROUND TRUTH from the live
        # tree — a cached/hinted/LLM guess must never override it. Root-cause fix
        # (2026-06-15, operator-found): this dropdown kept classifying
        # EXERCISE_TEXT_INPUT because _infer_exercise_subtype only ran as a
        # subtype FALLBACK inside canonicalize_screen_type (reached only for
        # non-registered variants); since EXERCISE_TEXT_INPUT is itself a
        # registered type, canonicalize returned it directly and the deterministic
        # DROPDOWN read never ran. The worker then correctly re-read the widget,
        # re-classified, and conformance rejected "worker changed screen_type" ->
        # loop. _infer_exercise_subtype returns UNKNOWN for ANY ambiguous shape,
        # so running it first can only ever CORRECT an unambiguous case, never
        # introduce a wrong guess.
        det_subtype = _infer_exercise_subtype(scoped_tree)
        if det_subtype != "UNKNOWN" and det_subtype in _load_screen_type_registry(platform):
            return {
                "success": True,
                "screen_type": det_subtype,
                "confidence_note": f"Deterministic answer-widget signature -> {det_subtype} (ground truth, overrides cache/LLM).",
                "platform_variant": det_subtype,
            }

        hash_result = lookup_by_hash(platform, skel_hash)
        if hash_result:
            canonical = canonicalize_screen_type(platform, hash_result.get("variant", "UNKNOWN"), scoped_tree)
            if canonical != "UNKNOWN":
                return {
                    "success": True,
                    "screen_type": canonical,
                    "confidence_note": f"Exact skeleton hash matched cached variant {hash_result.get('variant', 'UNKNOWN')}.",
                    "platform_variant": canonical,
                }

        sig_result = match_signature(platform, scoped_tree)
        if sig_result.get("matched"):
            canonical = canonicalize_screen_type(platform, sig_result.get("screen_type", "UNKNOWN"), scoped_tree)
            if canonical != "UNKNOWN":
                return {
                    "success": True,
                    "screen_type": canonical,
                    "confidence_note": f"Signature match mapped cached variant {sig_result.get('screen_type', 'UNKNOWN')}.",
                    "platform_variant": canonical,
                }

        pruned = prune_tree_for_prompt(scoped_tree)
        tree_json = json.dumps(pruned, indent=None, ensure_ascii=False)
        prompt = CLASSIFICATION_PROMPT.format(
            platform=platform,
            platform_context=platform_context,
            allowed_screen_types=allowed_screen_types,
            tree_json=tree_json,
        )
        raw = _llm_call(prompt, screenshot_b64)
        if not raw:
            raise RuntimeError("classification model returned nothing")
        if raw.startswith("```"):
            raw = raw.split("\n", 1)[1] if "\n" in raw else raw[3:]
            if raw.endswith("```"):
                raw = raw[:-3]
            raw = raw.strip()
        result = json.loads(raw)
        screen_type = canonicalize_screen_type(platform, result.get("screen_type", "UNKNOWN"), scoped_tree)
        return {
            "success": True,
            "screen_type": screen_type,
            "confidence_note": result.get("confidence_note", ""),
            "platform_variant": screen_type if screen_type != "UNKNOWN" else "",
        }
    except Exception as e:
        raw_head = (raw or "")[:200]
        logger.error(f"classify_screen failed: {e}; raw={raw_head}")
        return {
            "success": False,
            "screen_type": "UNKNOWN",
            "error": str(e),
        }


def _should_extract(screen_type: str) -> bool:
    if screen_type in _NO_EXTRACT_TYPES:
        return False
    from spark.tasks.screen_type_util import get_master_category

    master = get_master_category(screen_type)
    return master not in _NO_EXTRACT_TYPES


def build_extract_config(tree: dict, screenshot_b64: str, platform: str, screen_type: str):
    if not _should_extract(screen_type):
        return None

    scoped_tree = _find_web_area(tree)
    pruned = prune_tree_for_prompt(scoped_tree)
    tree_json = json.dumps(pruned, indent=None, ensure_ascii=False)
    prompt = _EXTRACT_PROMPT.format(
        platform=platform,
        screen_type=screen_type,
        tree_json=tree_json,
    )
    raw = _llm_call(prompt, screenshot_b64)
    if not raw:
        return None
    try:
        if raw.startswith("```"):
            raw = raw.split("\n", 1)[1] if "\n" in raw else raw[3:]
            if raw.endswith("```"):
                raw = raw[:-3]
            raw = raw.strip()
        if raw.lower() == "null":
            return None
        result = json.loads(raw)
        if not isinstance(result, dict):
            return None
        if "text" not in result or not result["text"]:
            return None
        return result
    except json.JSONDecodeError as e:
        logger.error(f"build_extract_config parse error: {e}, raw={raw[:200]}")
        return None


def _describe_screen(tree: dict) -> str:
    tags = analyze_tree(tree)
    web_area = _find_web_area(tree)
    counts = _count_roles(web_area)

    parts = []
    if tags:
        parts.append(f"Detected signals: {', '.join(tags)}")

    key_roles = [
        ("AXButton", "buttons"),
        ("AXLink", "links"),
        ("AXRadioButton", "radio buttons"),
        ("AXCheckBox", "checkboxes"),
        ("AXTextField", "text fields"),
        ("AXTextArea", "text areas"),
        ("AXComboBox", "dropdowns"),
        ("AXImage", "images"),
    ]
    for role, label in key_roles:
        count = counts.get(role, 0)
        if count > 0:
            parts.append(f"{count} {label}")

    button_names = []

    def walk(node):
        if not isinstance(node, dict):
            return
        role = node.get("role", "")
        name = node.get("name", "") or node.get("title", "")
        if role in ("AXButton", "AXLink") and name and len(name) < 60:
            button_names.append(f"{name} ({role})")
        for child in node.get("children", []):
            walk(child)

    walk(web_area)
    if button_names:
        parts.append(f"Key elements: {', '.join(button_names[:15])}")
    return "; ".join(parts) if parts else "No specific signals detected"
