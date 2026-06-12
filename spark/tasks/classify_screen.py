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

logger = logging.getLogger(__name__)

SCREEN_CATEGORIES = {
    "NAVIGATION": "Content list, module overview, course home, dashboard. Primary action: pick which content to go to next.",
    "VIDEO": "Video content delivery. May be unstarted, playing, or complete.",
    "ARTICLE": "Reading/text content page.",
    "EXERCISE": "Interactive assessment.",
    "TRANSITION": "Click-through screen: score card, start, continue, completion modal.",
    "UNKNOWN": "Does not fit any category above. Requires human review.",
}

CLASSIFICATION_PROMPT = """\
You are classifying a screen from an educational platform (LMS).

Look at the screenshot and accessibility tree below. Determine which ONE category
this screen belongs to.

=== CATEGORIES ===
{categories}

=== PLATFORM ===
{platform}

{platform_context}

=== ACCESSIBILITY TREE ===
{tree_json}

=== INSTRUCTIONS ===
1. Look at the screenshot first — what is the primary content on this page?
2. Check the accessibility tree for confirming signals (roles, button names, etc.)
3. Focus on the MAIN CONTENT AREA, not sidebars or browser chrome.
4. If you see a video player as the main content, it's VIDEO regardless of sidebar links.
5. If you see quiz questions/answers as the main content, it's EXERCISE regardless of sidebar links.
6. If you're not sure, return UNKNOWN. Do not guess.

=== RESPONSE FORMAT ===
Return ONLY valid JSON, no markdown, no explanation outside the JSON:
{{
  "screen_type": "NAVIGATION|VIDEO|ARTICLE|EXERCISE|TRANSITION|UNKNOWN",
  "confidence_note": "Brief 1-sentence explanation of why this classification",
  "platform_variant": "Optional platform-specific subtype if relevant, or empty string"
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


def _load_platform_context(platform: str) -> str:
    from spark.tasks.knowledge_loader import load_knowledge

    knowledge = load_knowledge(platform)
    if not knowledge:
        return ""

    parts = []
    screen_types = knowledge.get("screen_types", {})
    if screen_types:
        parts.append("=== KNOWN SCREEN TYPES ===\n" + ", ".join(sorted(screen_types.keys())))

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
        )
        return raw.strip()
    except ClaudeCallError as e:
        logger.error(f"LLM call failed: {e}")
        return None
    except Exception as e:
        logger.error(f"LLM call unexpected error: {e}")
        return None


def classify_screen(tree: dict, screenshot_b64: Optional[str], platform: str) -> dict:
    raw = ""
    try:
        categories_text = "\n".join(f"- **{name}**: {desc}" for name, desc in SCREEN_CATEGORIES.items())
        platform_context = _load_platform_context(platform)
        scoped_tree = _find_web_area(tree)
        pruned = prune_tree_for_prompt(scoped_tree)
        tree_json = json.dumps(pruned, indent=None, ensure_ascii=False)
        prompt = CLASSIFICATION_PROMPT.format(
            categories=categories_text,
            platform=platform,
            platform_context=platform_context,
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
        screen_type = result.get("screen_type", "UNKNOWN")
        if screen_type not in SCREEN_CATEGORIES:
            screen_type = "UNKNOWN"
        return {
            "success": True,
            "screen_type": screen_type,
            "confidence_note": result.get("confidence_note", ""),
            "platform_variant": result.get("platform_variant", ""),
        }
    except Exception as e:
        logger.error(f"classify_screen failed: {e}; raw={raw[:200]}")
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
