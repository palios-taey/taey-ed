"""
Screen classification and click target selection via Gemini API.

Hybrid approach:
  classify_screen() — "What type of screen is this?" → one of 6 categories
  get_click_target() — "What text should I click?" → simple string answer
  build_bt() — Deterministic BT construction from screen_type + click target
"""

import base64
import json
import logging
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# Universal screen categories — validated against IMS Caliper Analytics,
# edX XBlocks, Moodle activities, Canvas assessment types.
# See REBUILD_PLAN.md Part 10 for full rationale.
SCREEN_CATEGORIES = {
    "NAVIGATION": "Content list, module overview, course home, dashboard. Primary action: pick which content to go to next.",
    "VIDEO": "Video content delivery. May be unstarted (Play button visible), playing (Pause visible), or complete (checkmark, Next button).",
    "ARTICLE": "Reading/text content page. Static lesson content to read through. May have 'Mark as completed' button.",
    "EXERCISE": "Interactive assessment — any type: multiple choice (radio buttons), multi-select (checkboxes), fill-in-blank (text input), dropdown matching. Single or multi-question.",
    "TRANSITION": "Click-through screen: score card, 'Start quiz', 'Continue', 'Resume', completion message, confirmation modal. Single button click to advance.",
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
3. Focus on the MAIN CONTENT AREA, not sidebars or navigation chrome.
   Many pages have sidebars with links to all modules — that does NOT make it a NAVIGATION screen.
   A NAVIGATION screen is one where picking the next content item IS the primary action.
4. If you see a video player as the main content, it's VIDEO regardless of sidebar links.
5. If you see quiz questions/answers as the main content, it's EXERCISE regardless of sidebar links.
6. If you're not sure, return UNKNOWN. Do not guess.

=== RESPONSE FORMAT ===
Return ONLY valid JSON, no markdown, no explanation outside the JSON:
{{
  "screen_type": "NAVIGATION|VIDEO|ARTICLE|EXERCISE|TRANSITION|UNKNOWN",
  "confidence_note": "Brief 1-sentence explanation of why this classification",
  "platform_variant": "Optional platform-specific subtype if relevant, e.g. VIDEO_PLAYING, EXERCISE_RADIO, or empty string"
}}
"""


def _load_platform_context(platform: str) -> str:
    """Load platform-specific screen patterns from RESEARCH.md section 4 if available."""
    research_paths = [
        Path(__file__).parent.parent / "platforms" / platform / "RESEARCH.md",
        Path(f"spark/platforms/{platform}/RESEARCH.md"),
    ]

    for path in research_paths:
        if not path.exists():
            continue
        try:
            text = path.read_text()
            # Extract section 4 (Common Screen Patterns) if it exists
            lines = text.split("\n")
            in_section = False
            section_lines = []
            for line in lines:
                if line.startswith("## 4."):
                    in_section = True
                    section_lines.append(line)
                elif in_section and line.startswith("## ") and not line.startswith("## 4"):
                    break
                elif in_section:
                    section_lines.append(line)

            if section_lines:
                return "=== PLATFORM-SPECIFIC SCREEN PATTERNS ===\n" + "\n".join(section_lines)
        except Exception as e:
            logger.warning(f"Failed to load RESEARCH.md for {platform}: {e}")

    return ""


def classify_screen(
    tree: dict,
    screenshot_b64: Optional[str],
    platform: str,
) -> dict:
    """
    Classify a screen using Gemini API.

    Args:
        tree: Full accessibility tree from Mac capture
        screenshot_b64: Base64-encoded screenshot (optional but strongly preferred)
        platform: Platform name (e.g., "coursera", "khan_academy")

    Returns:
        {
            "success": True/False,
            "screen_type": "NAVIGATION|VIDEO|ARTICLE|EXERCISE|TRANSITION|UNKNOWN",
            "confidence_note": "...",
            "platform_variant": "...",
            "error": "..." (only if success=False)
        }
    """
    try:
        import google.generativeai as genai

        # Load API key
        secrets_path = Path(__file__).parent.parent / "palios-taey-secrets.json"
        if not secrets_path.exists():
            logger.error("classify_screen: palios-taey-secrets.json missing")
            return {
                "success": False,
                "screen_type": "UNKNOWN",
                "error": "Gemini API key not configured",
            }

        secrets = json.loads(secrets_path.read_text())
        api_key = secrets.get("gemini_api_key", "")
        if not api_key:
            logger.error("classify_screen: Gemini API key empty")
            return {
                "success": False,
                "screen_type": "UNKNOWN",
                "error": "Gemini API key empty",
            }

        genai.configure(api_key=api_key)

        # Build category descriptions
        categories_text = "\n".join(
            f"- **{name}**: {desc}" for name, desc in SCREEN_CATEGORIES.items()
        )

        # Load platform-specific context
        platform_context = _load_platform_context(platform)

        # Full tree as JSON (user requirement: send everything, we don't know what's important)
        tree_json = json.dumps(tree, indent=None, ensure_ascii=False)

        # Build prompt
        prompt = CLASSIFICATION_PROMPT.format(
            categories=categories_text,
            platform=platform,
            platform_context=platform_context,
            tree_json=tree_json,
        )

        # Build content parts: prompt + optional screenshot
        content_parts = [prompt]
        if screenshot_b64:
            try:
                image_data = base64.b64decode(screenshot_b64)
                mime_type = "image/png" if image_data[:8] == b'\x89PNG\r\n\x1a\n' else "image/jpeg"
                content_parts.append({"mime_type": mime_type, "data": image_data})
                logger.info("classify_screen: sending tree + screenshot to Gemini")
            except Exception as e:
                logger.warning(f"classify_screen: screenshot decode failed ({e}), sending tree only")
        else:
            logger.info("classify_screen: no screenshot, sending tree only")

        # Call Gemini
        model = genai.GenerativeModel("gemini-2.5-pro")
        response = model.generate_content(content_parts)
        raw = response.text.strip()

        logger.info(f"classify_screen: Gemini 2.5 Pro response len={len(raw)}")

        # Parse JSON response
        # Strip markdown code fences if present
        if raw.startswith("```"):
            raw = raw.split("\n", 1)[1] if "\n" in raw else raw[3:]
            if raw.endswith("```"):
                raw = raw[:-3]
            raw = raw.strip()

        result = json.loads(raw)
        screen_type = result.get("screen_type", "UNKNOWN")

        # Validate screen_type is one of our categories
        if screen_type not in SCREEN_CATEGORIES:
            logger.warning(
                f"classify_screen: Gemini returned invalid type '{screen_type}', "
                f"defaulting to UNKNOWN"
            )
            screen_type = "UNKNOWN"

        logger.info(
            f"classify_screen: type={screen_type} "
            f"variant={result.get('platform_variant', '')} "
            f"note={result.get('confidence_note', '')}"
        )

        return {
            "success": True,
            "screen_type": screen_type,
            "confidence_note": result.get("confidence_note", ""),
            "platform_variant": result.get("platform_variant", ""),
        }

    except json.JSONDecodeError as e:
        logger.error(f"classify_screen: Failed to parse Gemini JSON: {e}, raw={raw[:200]}")
        return {
            "success": False,
            "screen_type": "UNKNOWN",
            "error": f"JSON parse error: {e}",
        }
    except Exception as e:
        logger.error(f"classify_screen: Gemini API error: {e}")
        return {
            "success": False,
            "screen_type": "UNKNOWN",
            "error": str(e),
        }


def _gemini_api_call(prompt: str, screenshot_b64: Optional[str] = None,
                     model_name: str = "gemini-2.5-pro") -> Optional[str]:
    """Make a Gemini API call. Returns raw response text or None on error.

    Args:
        model_name: Which Gemini model to use. Default is gemini-2.5-pro.
    """
    try:
        import google.generativeai as genai

        secrets_path = Path(__file__).parent.parent / "palios-taey-secrets.json"
        if not secrets_path.exists():
            logger.error("Gemini API key not configured")
            return None

        secrets = json.loads(secrets_path.read_text())
        api_key = secrets.get("gemini_api_key", "")
        if not api_key:
            logger.error("Gemini API key empty")
            return None

        genai.configure(api_key=api_key)

        content_parts = [prompt]
        if screenshot_b64:
            try:
                image_data = base64.b64decode(screenshot_b64)
                mime_type = "image/png" if image_data[:8] == b'\x89PNG\r\n\x1a\n' else "image/jpeg"
                content_parts.append({"mime_type": mime_type, "data": image_data})
            except Exception:
                pass

        model = genai.GenerativeModel(model_name)
        logger.info(f"Gemini API call: model={model_name}, prompt_len={len(prompt)}")
        response = model.generate_content(content_parts)
        return response.text.strip()

    except Exception as e:
        logger.error(f"Gemini API error ({model_name}): {e}")
        return None


# ── Click Target Selection ──
# Gemini answers ONE simple question: "what text should I click?"
# No BT construction, no roles, no strategies — just the target text.

CLICK_TARGET_PROMPT = """\
You are looking at a {screen_type} screen on {platform} (educational LMS).

Look at the screenshot and accessibility tree. Answer ONE question:
What is the exact text of the element I should click to proceed?

Rules:
- For NAVIGATION: identify the next INCOMPLETE content item (not completed ones).
  Look for items WITHOUT checkmarks or "Completed" indicators.
- For TRANSITION: identify the button that advances (e.g., "Next", "Continue", "Start quiz").
  NEVER click "Skip" or "Up next".
- Return ONLY the exact text as it appears on screen. Nothing else.
- If there's a "Resume" link/button, return its associated content item text instead
  (the item name, not the word "Resume").

=== ACCESSIBILITY TREE ===
{tree_json}

Return ONLY the exact click target text, no JSON, no explanation.
"""


def get_click_target(
    tree: dict,
    screenshot_b64: Optional[str],
    platform: str,
    screen_type: str,
) -> Optional[str]:
    """
    Ask Gemini: "what text should I click?" Returns just a string.

    Used for NAVIGATION and TRANSITION screens where the BT structure
    is fixed but the click target varies per screen.
    """
    tree_json = json.dumps(tree, indent=None, ensure_ascii=False)
    prompt = CLICK_TARGET_PROMPT.format(
        screen_type=screen_type,
        platform=platform,
        tree_json=tree_json,
    )

    logger.info(f"get_click_target: asking Gemini for {screen_type} click target")
    raw = _gemini_api_call(prompt, screenshot_b64)

    if not raw:
        return None

    # Strip any quotes or whitespace
    target = raw.strip().strip('"').strip("'").strip()
    logger.info(f"get_click_target: Gemini says click '{target}'")
    return target


# ── Deterministic BT Templates ──
# Fixed structures per screen type. No Gemini involvement in BT construction.
# Uses "fallback" nodes for optional steps (try first child, if fails try next).
# Includes "extract" configs so Mac pipeline captures content via handle_extraction().

# Minimal fallback extract config — ONLY used when Gemini doesn't return one.
# Gemini builds targeted extraction configs dynamically by analyzing the actual tree.
_CONTENT_EXTRACT = {
    "text": [{"role": "AXStaticText"}],
    "images": [{"source": "window"}],
}


def build_bt(screen_type: str, click_target: Optional[str] = None) -> dict:
    """
    Build a deterministic behavior tree from screen_type.

    Args:
        screen_type: One of NAVIGATION, ARTICLE, VIDEO, EXERCISE, TRANSITION, UNKNOWN
        click_target: For NAVIGATION/TRANSITION — the text to click (from Gemini)

    Returns:
        {"tree": {...}, "screen_type": "...", "expected_next": [...], "extract": {...}}
    """
    if screen_type == "VIDEO":
        return {
            "tree": {
                "type": "sequence",
                "children": [
                    {"type": "action", "action": "video_poll"},
                ],
            },
            "extract": _CONTENT_EXTRACT,
            "screen_type": "VIDEO_PLAYING",
            "expected_next": ["ARTICLE", "EXERCISE", "NAVIGATION", "TRANSITION"],
        }

    if screen_type == "ARTICLE":
        return {
            "tree": {
                "type": "sequence",
                "children": [
                    # Scroll to bottom to reach Mark as completed button
                    {"type": "action", "action": "scroll", "params": {
                        "direction": "down", "amount": 50,
                    }},
                    # Wait for Chrome to update accessibility tree after scroll
                    {"type": "action", "action": "wait", "params": {"seconds": 1.5}},
                    # OPTIONAL: Mark as completed (may not exist on already-completed articles).
                    # fallback = try first child, if fails try next. wait(0.5) always succeeds.
                    {"type": "fallback", "children": [
                        {"type": "sequence", "children": [
                            {"type": "action", "action": "find_and_click", "params": {
                                "target": "Mark as completed",
                                "strategy": "mouse_click",
                                "match_mode": "contains",
                            }},
                            # Wait for page to update after marking complete
                            {"type": "action", "action": "wait", "params": {"seconds": 2.0}},
                        ]},
                        {"type": "action", "action": "wait", "params": {"seconds": 0.5}},
                    ]},
                    # Navigate forward
                    {"type": "action", "action": "find_and_click", "params": {
                        "target": "Go to next item",
                        "strategy": "mouse_click",
                        "match_mode": "contains",
                        "post_delay": 3.0,
                    }},
                ],
            },
            "extract": _CONTENT_EXTRACT,
            "screen_type": "ARTICLE_READING",
            "expected_next": ["ARTICLE", "VIDEO", "EXERCISE", "NAVIGATION", "TRANSITION"],
        }

    if screen_type == "EXERCISE":
        # Top-level fallback: try to solve the exercise, if that fails
        # (e.g., discussion prompt, unfamiliar format), mark complete and advance.
        return {
            "tree": {
                "type": "fallback",
                "children": [
                    # Option 1: Solve the exercise (multiple-choice, checkbox, etc.)
                    {"type": "sequence", "children": [
                        {"type": "action", "action": "extract_question", "store": "q"},
                        {"type": "action", "action": "send_to_llm", "params": {
                            "question": "$q.question_text",
                            "options": "$q.options",
                            "question_type": "solve_choice",
                            "context": "$q.reference_texts",
                        }, "store": "llm"},
                        {"type": "action", "action": "find_and_click", "params": {
                            "target": "$llm.answer",
                            "strategy": "focus_space",
                            "match_mode": "contains",
                        }},
                        {"type": "action", "action": "find_and_click", "params": {
                            "target": "Submit",
                            "strategy": "mouse_click",
                            "match_mode": "contains",
                            "post_delay": 2.0,
                        }},
                        {"type": "action", "action": "store_qa", "params": {
                            "question": "$q.question_text",
                            "answer": "$llm.answer",
                            "question_type": "solve_choice",
                        }},
                    ]},
                    # Option 2: Discussion prompt — type a reply and submit
                    {"type": "sequence", "children": [
                        {"type": "action", "action": "extract_question", "store": "q"},
                        {"type": "action", "action": "send_to_llm", "params": {
                            "question": "$q.question_text",
                            "question_type": "solve_open",
                            "context": "$q.reference_texts",
                        }, "store": "llm"},
                        {"type": "action", "action": "find_and_type", "params": {
                            "target": "Your Answer",
                            "text": "$llm.answer",
                            "strategy": "mouse_click",
                            "match_mode": "contains",
                        }},
                        {"type": "action", "action": "find_and_click", "params": {
                            "target": "Submit",
                            "strategy": "mouse_click",
                            "match_mode": "contains",
                            "post_delay": 2.0,
                        }},
                    ]},
                    # Option 3: Can't solve — mark complete and advance.
                    # Content was already extracted before BT runs.
                    {"type": "sequence", "children": [
                        {"type": "fallback", "children": [
                            {"type": "action", "action": "find_and_click", "params": {
                                "target": "Mark as completed",
                                "strategy": "mouse_click",
                                "match_mode": "contains",
                                "post_delay": 1.0,
                            }},
                            {"type": "action", "action": "wait", "params": {"seconds": 0.5}},
                        ]},
                        {"type": "action", "action": "find_and_click", "params": {
                            "target": "Go to next item",
                            "strategy": "mouse_click",
                            "match_mode": "contains",
                            "post_delay": 3.0,
                        }},
                    ]},
                ],
            },
            "extract": _CONTENT_EXTRACT,
            "screen_type": "EXERCISE_SOLVING",
            "expected_next": ["EXERCISE", "TRANSITION", "NAVIGATION"],
        }

    if screen_type == "NAVIGATION" and click_target:
        return {
            "tree": {
                "type": "sequence",
                "children": [
                    {"type": "action", "action": "find_and_click", "params": {
                        "target": click_target,
                        "strategy": "mouse_click",
                        "match_mode": "contains",
                        "post_delay": 3.0,
                    }},
                ],
            },
            "screen_type": "NAVIGATION",
            "expected_next": ["ARTICLE", "VIDEO", "EXERCISE", "TRANSITION"],
        }

    if screen_type == "TRANSITION" and click_target:
        return {
            "tree": {
                "type": "sequence",
                "children": [
                    {"type": "action", "action": "find_and_click", "params": {
                        "target": click_target,
                        "strategy": "mouse_click",
                        "match_mode": "contains",
                        "post_delay": 3.0,
                    }},
                ],
            },
            "screen_type": "TRANSITION",
            "expected_next": ["ARTICLE", "VIDEO", "EXERCISE", "NAVIGATION"],
        }

    # Fallback for UNKNOWN or missing click_target
    return None


# ── Gemini 3 Pro BT Builder ──
# Primary path for ALL screens. Gemini 3 Pro analyzes the actual tree + screenshot
# and builds a screen-specific BT using the full handler reference from prompt_codex.
# Each BT is stored with the screen signature — built once, reused forever.

_BT_BUILDER_PROMPT = """\
You are an automation engineer building a behavior tree (BT) for an educational
platform. This BT will be executed by a Mac app via accessibility APIs (AXButton,
AXLink, AXRadioButton, etc.).

PLATFORM: {platform}
CLASSIFIED SCREEN TYPE: {screen_type}

=== YOUR GOAL ===
Look at the screenshot and accessibility tree. Build a BT that COMPLETES this
screen — meaning: do whatever this screen requires, then advance to the next one.

Every screen on an LMS needs to be completed. That means:
- VIDEO: Watch it to the end (video_poll), then find and click the completion button, then the advance button
- ARTICLE/READING: Scroll to bottom (amount=50 to ensure full page), wait 1s, then find and click the
  completion/mark-complete button (REQUIRED — not optional), wait 2s for page to update, THEN find and
  click the advance/next button. Completion MUST succeed before advancing. Look at the tree for actual
  button names — they vary by platform.
- EXERCISE: Figure out what kind of exercise it is by examining the tree:
  * Radio buttons (AXRadioButton) → multiple choice: extract_question → send_to_llm(solve_choice) → click answer → submit
  * Checkboxes (AXCheckBox) → multi-select: extract_question → send_to_llm(solve_checkbox) → for_each click → submit
  * Text area (AXTextArea) → open response: extract_question → send_to_llm(solve) → find_and_type the answer → submit
  * Discussion prompt → type a response and submit
  * Dropdowns (AXComboBox/AXPopUpButton) → matching: find_all → send_to_llm(solve_matching) → for_each select → submit
  After solving, find the submit button in the tree (text varies by platform), then try to advance
- NAVIGATION: Find all links, use send_to_llm(navigate) to pick next incomplete item, click it
- TRANSITION: Find and click the button that advances — look at the tree for the actual button text

=== CRITICAL RULES ===
1. ONLY use handlers listed in the HANDLER REFERENCE below. Any unlisted action SILENTLY FAILS.
2. Use element NAME (text) to find elements, NEVER element_id. IDs are for your reference only.
3. Click strategies for BROWSER platforms (Chrome): "mouse_click" for buttons and links (ax_press SILENTLY FAILS on Chrome).
   "focus_space" for radio buttons and checkboxes. "focus_enter" also works for buttons.
   IMPORTANT: After a scroll action, the element positions change. Always use a wait(1.0) after scroll
   before any find_and_click so Chrome can update the accessibility tree positions.
4. video_poll must be the ONLY child in its sequence (it sleeps 30s, returns continue_loop=true).
5. for_each/conditional: put items/variable/do/condition/then/else at TOP LEVEL, NOT inside params.
6. NEVER hardcode link text as click targets for navigation — use $nav.answer from send_to_llm.
7. NEVER click "Skip" or "Up next" buttons.
8. Use "fallback" for try-or-skip nodes (NOT "selector"). Our system uses "fallback" as the keyword.
9. Button names vary by platform. ALWAYS look at the accessibility tree to find the actual submit,
   complete, and advance button names. NEVER assume button text.
10. Sidebar links are CHROME, not main content. An ARTICLE screen needs scroll+complete, not navigate.
11. For ARTICLE screens: the completion button must be in a SEQUENCE (not fallback). It MUST be clicked
    before the advance button — put a wait(2.0) between them. Scroll amount=50 to reach bottom.
12. NEVER use strategy "ax_press" for browser elements. Chrome ignores AXPress — always use "mouse_click"
    for buttons/links or "focus_space" for radio/checkboxes.

=== HOW TO READ THE TREE ===
The accessibility tree is a JSON hierarchy. Focus on the AXWebArea subtree (the web page content).
Skip browser chrome: AXMenuBar, AXToolbar, AXTabGroup.

Key signals to look for:
- AXRadioButton elements → multiple choice question
- AXCheckBox elements → multi-select question
- AXTextArea/AXTextField → text input (fill-in-blank, discussion, open response)
- AXComboBox/AXPopUpButton → dropdown matching exercise
- AXVideo/AXMediaTimeline → video player
- AXLink (many, 15+) → navigation/content list
- AXButton/AXLink with action-related text → completion, submit, advance buttons (READ the actual names)
- "Play"/"Pause" buttons → video state detection

{screen_specific_guidance}
"""

_BT_BUILDER_RESPONSE = """\
=== RESPONSE FORMAT ===
Return ONLY valid JSON. No markdown fences. No explanation outside the JSON object.
{{
  "screen_type": "DESCRIPTIVE_NAME_LIKE_EXERCISE_RADIO_OR_ARTICLE_READING",
  "tree": {{
    "type": "sequence",
    "children": [...]
  }},
  "extract": {{
    "text": [...],
    "images": [...]
  }},
  "expected_next": ["SCREEN_TYPE_AFTER_THIS"],
  "reason": "One sentence: what this BT does and why"
}}

RULES:
- screen_type: descriptive name for this specific screen (EXERCISE_RADIO, ARTICLE_READING, etc.)
- tree: valid BT with type "sequence" at root. Use "fallback" for optional steps.
- expected_next: what screen types appear after this BT runs. NEVER include current screen type.

=== EXTRACTION CONFIG (extract field) ===
Extract captures the UNIQUE CONTENT of this screen for learning records. NOT page chrome,
sidebars, or navigation. YOU must analyze the accessibility tree to determine what the
unique content is and WHERE it lives in the tree, then build targeted extraction criteria.

TEXT EXTRACTION CRITERIA:
Each criteria object matches nodes in the accessibility tree. Available filters:
  - "role" (required): AX role to match, e.g. "AXStaticText"
  - "parent_role" (optional): Only match if parent has this role, e.g. "AXGroup"
  - "parent_contains" (optional): Only match if parent's name contains this string
    LOOK AT THE TREE to find the actual parent container names for the content area.
    Examples: "transcript", "lesson-content", "article-body", "reading", "question"
  - "contains" (optional): Only match if the text value contains this substring
  - "min_length" (optional): Skip text shorter than this (filters button labels, nav links)

Examples:
  Video with transcript section:
    {{"text": [{{"role": "AXStaticText", "parent_contains": "transcript"}}],
     "images": [{{"source": "window", "purpose": "Describe the video content being shown"}}]}}

  Article inside a content container:
    {{"text": [{{"role": "AXStaticText", "parent_contains": "lesson-content", "min_length": 20}}],
     "images": [{{"source": "window", "purpose": "Describe the article content"}}]}}

  Exercise with questions:
    {{"text": [{{"role": "AXStaticText", "parent_contains": "question", "min_length": 10}}],
     "images": [{{"source": "window", "purpose": "Describe the exercise"}}]}}

  Generic content (when you can't find a specific parent):
    {{"text": [{{"role": "AXStaticText", "min_length": 40}}],
     "images": [{{"source": "window"}}]}}

IMAGE EXTRACTION OPTIONS:
  - {{"source": "window"}}: Full window screenshot sent to VLM for description
  - {{"source": "window", "purpose": "..."}}: Same but with purpose context for VLM
  - {{"role": "AXImage", "name": "diagram"}}: Find element in tree, crop its region
  - {{"bbox": [x, y, w, h]}}: Crop a specific region

HOW TO BUILD GOOD EXTRACTION:
1. Look at the accessibility tree for the content area structure
2. Find the parent container that holds the unique content (article body, transcript, question)
3. Use parent_contains to scope extraction to that container
4. Use min_length to filter out short labels if needed
5. Always include an image extraction for VLM to describe visual content
6. NAVIGATION/TRANSITION screens: omit extract (no unique content to capture)
"""

# Valid handler names for BT validation
_VALID_HANDLERS = {
    "find_and_click", "find_and_type", "find_all", "click",
    "extract_question", "send_to_llm", "video_poll", "wait",
    "press_key", "scroll", "wait_for_element", "discover_menu",
    "lookup_match", "store_qa", "solve_assessment_page",
    "press_escape", "for_each", "conditional",
}


def _normalize_bt(tree: dict) -> dict:
    """Normalize BT node types (e.g., 'selector' → 'fallback')."""
    if not isinstance(tree, dict):
        return tree
    tree = dict(tree)  # shallow copy
    # Standard BT literature calls fallback nodes "selector" — normalize
    if tree.get("type") == "selector":
        tree["type"] = "fallback"
    if "children" in tree:
        tree["children"] = [_normalize_bt(c) for c in tree["children"]]
    for key in ("do", "then", "else"):
        if key in tree and isinstance(tree[key], dict):
            tree[key] = _normalize_bt(tree[key])
    return tree


def _validate_bt(tree: dict) -> bool:
    """Validate that a BT only uses registered handlers."""
    if not isinstance(tree, dict):
        return False
    node_type = tree.get("type", "")
    if node_type in ("sequence", "fallback"):
        children = tree.get("children", [])
        return all(_validate_bt(c) for c in children)
    elif node_type == "action":
        action = tree.get("action", "")
        if action not in _VALID_HANDLERS:
            logger.warning(f"BT validation: unknown handler '{action}'")
            return False
        # Check for_each/conditional nested trees
        if action == "for_each":
            do_node = tree.get("do")
            if do_node and not _validate_bt(do_node):
                return False
        elif action == "conditional":
            for key in ("then", "else"):
                sub = tree.get(key)
                if sub and not _validate_bt(sub):
                    return False
        return True
    return False


def _describe_screen(tree: dict) -> str:
    """Generate a human-readable description of key screen elements."""
    from spark.tasks.prompt_codex import analyze_tree, _find_web_area, _count_roles

    tags = analyze_tree(tree)
    web_area = _find_web_area(tree)
    counts = _count_roles(web_area)

    parts = []
    if tags:
        parts.append(f"Detected signals: {', '.join(tags)}")

    # Summarize key elements
    key_roles = [
        ("AXButton", "buttons"), ("AXLink", "links"),
        ("AXRadioButton", "radio buttons"), ("AXCheckBox", "checkboxes"),
        ("AXTextField", "text fields"), ("AXTextArea", "text areas"),
        ("AXComboBox", "dropdowns"), ("AXImage", "images"),
    ]
    for role, label in key_roles:
        count = counts.get(role, 0)
        if count > 0:
            parts.append(f"{count} {label}")

    # Extract visible button/link names
    button_names = []

    def walk(n):
        if not isinstance(n, dict):
            return
        role = n.get("role", "")
        name = n.get("name", "") or n.get("title", "")
        if role in ("AXButton", "AXLink") and name and len(name) < 60:
            button_names.append(f"{name} ({role})")
        for child in n.get("children", []):
            walk(child)

    walk(web_area)
    if button_names:
        parts.append(f"Key elements: {', '.join(button_names[:15])}")

    return "; ".join(parts) if parts else "No specific signals detected"


def build_bt_from_tree(
    tree: dict,
    screenshot_b64: Optional[str],
    platform: str,
    screen_type: str = "UNKNOWN",
    user_guidance: Optional[str] = None,
    failed_bt: Optional[dict] = None,
    failed_bt_debug: Optional[str] = None,
) -> Optional[dict]:
    """
    Build a BT using Gemini 2.5 Pro with full handler context from prompt_codex.

    PRIMARY PATH for all screens. Gemini analyzes the actual tree + screenshot
    and builds a screen-specific BT. For deterministic types (VIDEO, ARTICLE),
    the BT is stored with the signature. For all others, it's built fresh each time.

    Args:
        tree: Full accessibility tree from Mac
        screenshot_b64: Base64 screenshot (strongly recommended)
        platform: Platform name
        screen_type: Classified type (VIDEO, ARTICLE, EXERCISE, NAVIGATION, TRANSITION, UNKNOWN)
        user_guidance: Optional natural language from user ("click Reply button")
        failed_bt: Optional BT that was previously tried and failed
        failed_bt_debug: Optional execution log from the failed BT

    Returns:
        {"tree": {...}, "screen_type": "...", "expected_next": [...], "extract": {...}}
        or None if Gemini can't build a valid BT.
    """
    from spark.tasks.prompt_codex import (
        analyze_tree, SCREEN_PATTERNS, SECTION_5_HANDLERS,
        SECTION_6_QUESTION_TYPES, SECTION_7_STRATEGIES,
        load_research_sections,
    )

    tags = analyze_tree(tree)
    logger.info(
        f"build_bt_from_tree: platform={platform} type={screen_type} "
        f"tags={tags} has_guidance={'yes' if user_guidance else 'no'}"
    )

    # Build screen-specific guidance from detected tree signals
    # These are proven BT patterns with full explanations
    screen_guidance_parts = []
    for tag in tags:
        if tag in SCREEN_PATTERNS:
            screen_guidance_parts.append(SCREEN_PATTERNS[tag])

    screen_specific_guidance = "\n\n".join(screen_guidance_parts) if screen_guidance_parts else \
        "No specific assessment signals detected in the tree. Use the screenshot and tree to determine the right approach."

    # Assemble the complete prompt
    prompt_parts = [
        _BT_BUILDER_PROMPT.format(
            platform=platform,
            screen_type=screen_type,
            screen_specific_guidance=screen_specific_guidance,
        ),
    ]

    # Handler reference — the complete manual
    prompt_parts.append(SECTION_5_HANDLERS)
    prompt_parts.append(SECTION_6_QUESTION_TYPES)
    prompt_parts.append(SECTION_7_STRATEGIES)

    # Platform-specific knowledge from RESEARCH.md
    research = load_research_sections(platform, tags)
    if research:
        prompt_parts.append(f"=== PLATFORM KNOWLEDGE ({platform}) ===\n\n{research}")

    # User guidance — the teaching moment
    if user_guidance:
        prompt_parts.append(
            f"=== USER GUIDANCE ===\n"
            f"The user has told you exactly what to do on this screen:\n"
            f'"{user_guidance}"\n\n'
            f"Build a BT that follows these instructions precisely.\n"
            f"Use the accessibility tree to find the exact element names and roles.\n"
            f"The user's guidance overrides the screen type classification if they conflict."
        )

    # Failed BT context — tell Gemini what was tried and failed
    if failed_bt:
        failed_bt_json = json.dumps(failed_bt, indent=2)
        failed_section = (
            f"=== FAILED BEHAVIOR TREE (DO NOT REPEAT) ===\n"
            f"The following BT was executed and FAILED. You MUST build a "
            f"fundamentally different approach. Do not tweak parameters — "
            f"change the strategy entirely.\n\n"
            f"Failed BT:\n{failed_bt_json}\n"
        )
        if failed_bt_debug:
            failed_section += (
                f"\nExecution log (last lines):\n{failed_bt_debug}\n"
                f"Analyze what went wrong and build a BT that avoids this failure."
            )
        prompt_parts.append(failed_section)

    # The actual accessibility tree
    tree_json = json.dumps(tree, indent=None, ensure_ascii=False)
    prompt_parts.append(f"=== ACCESSIBILITY TREE ===\n{tree_json}")

    # Response format
    prompt_parts.append(_BT_BUILDER_RESPONSE)

    prompt = "\n\n".join(prompt_parts)
    logger.info(f"build_bt_from_tree: prompt length={len(prompt)} chars")

    # Call Gemini 2.5 Pro — BT building requires deep reasoning
    raw = _gemini_api_call(prompt, screenshot_b64, model_name="gemini-2.5-pro")
    if not raw:
        logger.error("build_bt_from_tree: Gemini 2.5 Pro returned nothing")
        return None

    # Parse JSON response
    try:
        if raw.startswith("```"):
            raw = raw.split("\n", 1)[1] if "\n" in raw else raw[3:]
            if raw.endswith("```"):
                raw = raw[:-3]
            raw = raw.strip()

        result = json.loads(raw)
    except json.JSONDecodeError as e:
        logger.error(f"build_bt_from_tree: JSON parse error: {e}, raw={raw[:300]}")
        return None

    # Normalize and validate the BT
    bt = result.get("tree")
    if not bt:
        logger.error("build_bt_from_tree: no 'tree' in response")
        return None

    bt = _normalize_bt(bt)

    if not _validate_bt(bt):
        logger.error(f"build_bt_from_tree: BT validation failed: {json.dumps(bt)[:500]}")
        return None

    bt_screen_type = result.get("screen_type", screen_type)
    logger.info(
        f"build_bt_from_tree: SUCCESS — type={bt_screen_type} "
        f"reason={result.get('reason', '')}"
    )

    return {
        "tree": bt,
        "screen_type": bt_screen_type,
        "extract": result.get("extract", _CONTENT_EXTRACT),
        "expected_next": result.get("expected_next", []),
    }
