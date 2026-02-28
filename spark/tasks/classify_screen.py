"""
Screen classification, click target selection, and BT building via Gemini API.

  classify_screen()     — "What type of screen is this?" → one of 6 categories
  get_click_target()    — "What text should I click?" → simple string answer
  build_bt_from_tree()  — Gemini builds a screen-specific BT from tree + screenshot
"""

import base64
import json
import logging
from pathlib import Path
from typing import Optional

from spark.tasks.prune_tree import prune_tree_for_prompt

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
    """Load platform-specific screen context from knowledge.json if available."""
    from spark.tasks.knowledge_loader import load_knowledge

    knowledge = load_knowledge(platform)
    if not knowledge:
        return ""

    parts = []

    # Screen types summary
    screen_types = knowledge.get("screen_types", {})
    if screen_types:
        type_names = sorted(screen_types.keys())
        parts.append(f"=== KNOWN SCREEN TYPES ===\n" + ", ".join(type_names))

    # Global platform quirks
    quirks = knowledge.get("global", {}).get("platform_quirks", [])
    if quirks:
        quirk_lines = []
        for q in quirks:
            quirk_lines.append(f"- {q.get('quirk', '')}: {q.get('workaround', '')} (affects: {', '.join(q.get('affects', []))})")
        parts.append("=== PLATFORM QUIRKS ===\n" + "\n".join(quirk_lines))

    return "\n\n".join(parts)


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

        # Inject completion indicators from knowledge.json if available
        from spark.tasks.knowledge_loader import load_knowledge
        knowledge = load_knowledge(platform)
        if knowledge:
            tree_guide = knowledge.get("accessibility_tree_guide", {})
            indicators = tree_guide.get("completion_indicators_in_tree", {})
            if indicators:
                indicator_text = "\n".join(f"- {k}: {v}" for k, v in indicators.items())
                platform_context += (
                    f"\n\n=== COMPLETION INDICATORS IN TREE ===\n{indicator_text}"
                )

        # Pruned tree for prompt (strip coordinates, element_id, redundant fields)
        pruned = prune_tree_for_prompt(tree)
        tree_json = json.dumps(pruned, indent=None, ensure_ascii=False)

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
    pruned = prune_tree_for_prompt(tree)
    tree_json = json.dumps(pruned, indent=None, ensure_ascii=False)
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


# Screen types that should NEVER have extraction (no unique content).
_NO_EXTRACT_TYPES = {"NAVIGATION", "TRANSITION"}


def _should_extract(screen_type: str) -> bool:
    """Return False for screen types that have no unique content to extract."""
    if screen_type in _NO_EXTRACT_TYPES:
        return False
    from spark.tasks.screen_type_util import get_master_category
    master = get_master_category(screen_type)
    return master not in _NO_EXTRACT_TYPES


def _get_extract_default(screen_type: str):
    """Get extract config for a screen type. Returns None for non-content screens."""
    if not _should_extract(screen_type):
        return None
    # No hardcoded fallback — caller should use build_extract_config() instead
    return None


# ── Dedicated Gemini extraction call ──
# Separate from BT building so Gemini can focus on one thing.

_EXTRACT_PROMPT = """\
You are analyzing an educational platform screen to determine what UNIQUE CONTENT
should be extracted for the student's learning records.

PLATFORM: {platform}
SCREEN TYPE: {screen_type}

=== WHAT TO EXTRACT ===
Capture the educational content that a student would want to review later.
NOT page chrome, sidebars, navigation menus, or UI labels.

For each screen type:
- ARTICLE/READING: The lesson text, headings, and key paragraphs in the main content area
- VIDEO: The transcript text (if a transcript panel exists), video title, lesson context
- EXERCISE: The question text, answer options, instructions, reference material
- DISCUSSION: The prompt text, any peer responses shown
- Other content screens: Whatever the primary educational content is

=== ACCESSIBILITY TREE ===
{tree_json}

=== YOUR TASK ===
1. Look at the screenshot and tree to find the MAIN CONTENT AREA
2. Identify the parent container that holds the unique content
   (look for containers named things like: "lesson", "content", "reading",
   "transcript", "question", "article", "main", etc.)
3. Build an extraction config that targets ONLY that content area

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
      "purpose": "<what educational content to describe — ignore stock photos>"
    }}
  ]
}}

CRITICAL RULES:
- Use "parent_contains" to scope to the content area. This is the MOST IMPORTANT filter.
  Look at the tree structure to find the right container name.
- If you cannot find a specific content container, use "parent_contains" with the
  broadest content-area parent you can identify (e.g., "main", "content", "body").
- For images: only describe educational content (diagrams, equations, charts, figures).
  Say "Ignore decorative and stock photos" in the purpose if the page has non-educational images.
- Return null (not an empty object) if there is genuinely no content to extract
  (e.g., a loading screen, error page, or empty state).
"""


def build_extract_config(
    tree: dict,
    screenshot_b64: str,
    platform: str,
    screen_type: str,
):
    """
    Dedicated Gemini call to build an extraction config for a screen.

    Separate from BT building so Gemini focuses entirely on identifying
    what content exists and where it lives in the tree.

    Returns:
        Extract config dict, or None if Gemini can't identify content.
    """
    import json

    if not _should_extract(screen_type):
        return None

    pruned = prune_tree_for_prompt(tree)
    tree_json = json.dumps(pruned, indent=None, ensure_ascii=False)
    prompt = _EXTRACT_PROMPT.format(
        platform=platform,
        screen_type=screen_type,
        tree_json=tree_json,
    )

    logger.info(f"build_extract_config: asking Gemini for {screen_type} extraction config")
    raw = _gemini_api_call(prompt, screenshot_b64, model_name="gemini-2.5-flash")

    if not raw:
        logger.warning("build_extract_config: Gemini returned nothing")
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

        # Validate it has at least text criteria
        if "text" not in result or not result["text"]:
            logger.warning("build_extract_config: Gemini returned extract without text criteria")
            return None

        logger.info(
            f"build_extract_config: got config with "
            f"{len(result.get('text', []))} text criteria, "
            f"{len(result.get('images', []))} image criteria"
        )
        return result

    except json.JSONDecodeError as e:
        logger.error(f"build_extract_config: JSON parse error: {e}, raw={raw[:200]}")
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

IMPORTANT: If your BT uses extract_question, the extract field MUST include "question"
criteria so the handler knows how to find the question/prompt in the tree.

EXTRACT FIELD KEYS:
  "question" (dict, REQUIRED for exercise screens): Criteria to find the question/prompt text
    - "role" (str): AX role to match (default: "AXStaticText")
    - "contains" (str): Only match if text contains this substring
    - "parent_contains" (str): Only match if parent name contains this string
    - "min_length" (int): Minimum text length to qualify as a question
  "options" (dict, for multiple-choice): Criteria to find answer option elements
    - "role" (str): AX role (e.g., "AXRadioButton", "AXCheckBox", "AXButton")
    - "exclude_titles" (list[str]): Button names to skip (e.g., ["Back", "Menu", "Close"])
  "text" (list[dict]): Reference/context text extraction criteria
    Each criteria object matches nodes in the accessibility tree. Available filters:
      - "role" (required): AX role to match, e.g. "AXStaticText"
      - "parent_role" (optional): Only match if parent has this role, e.g. "AXGroup"
      - "parent_contains" (optional): Only match if parent's name contains this string
        LOOK AT THE TREE to find the actual parent container names for the content area.
      - "contains" (optional): Only match if the text value contains this substring
      - "min_length" (optional): Skip text shorter than this (filters button labels, nav links)
  "images" (list[dict]): Image extraction options

CRITICAL: Do NOT guess parent container names. LOOK AT THE ACTUAL TREE above to find real
container names, roles, and text patterns for THIS screen. Every platform is different.

Examples (illustrative structure only — replace values with what you see in the tree):
  Content with identifiable parent container:
    {{"text": [{{"role": "AXStaticText", "parent_contains": "ACTUAL_PARENT_NAME_FROM_TREE", "min_length": 20}}],
     "images": [{{"source": "window", "purpose": "Describe the content"}}]}}

  Exercise screen (when BT uses extract_question):
    {{"question": {{"role": "AXStaticText", "min_length": 20}},
     "options": {{"role": "ACTUAL_OPTION_ROLE_FROM_TREE"}},
     "text": [{{"role": "AXStaticText", "min_length": 10}}],
     "images": [{{"source": "window", "purpose": "Describe the exercise"}}]}}

  Fallback (no clear parent container):
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


def _build_knowledge_context(
    knowledge: dict, screen_type: str, quirks: list, learned: dict
) -> str:
    """
    Build the knowledge context block for Gemini.
    Ordering is intentional — general → specific → learned (most recent context last).
    """
    parts = []

    # 1. Global timing
    timing = knowledge.get("global", {}).get("timing", {})
    if timing:
        timing_text = "\n".join(f"  {k}: {v}s" for k, v in timing.items() if k != "notes")
        notes = timing.get("notes", "")
        if notes:
            timing_text += f"\n  Note: {notes}"
        parts.append(f"=== PLATFORM TIMING ===\n{timing_text}")

    # 2. Never-click buttons
    never_click = knowledge.get("global", {}).get("never_click", [])
    if never_click:
        nc_text = "\n".join(
            f"- NEVER click \"{nc['text']}\": {nc['reason']}" for nc in never_click
        )
        parts.append(f"=== NEVER CLICK ===\n{nc_text}")

    # 3. Screen-type-specific quirks (filtered)
    if quirks:
        q_text = "\n".join(
            f"- [{q.get('severity', 'important').upper()}] {q['description']}"
            for q in quirks
        )
        parts.append(f"=== PLATFORM QUIRKS (affecting {screen_type}) ===\n{q_text}")

    # 4. Screen type specifics from knowledge
    screen_info = knowledge.get("screen_types", {}).get(screen_type, {})
    if screen_info:
        # Submit button
        submit = screen_info.get("submit_button")
        if submit:
            parts.append(
                f"=== SUBMIT BUTTON ===\n"
                f"Text: \"{submit['text']}\", Role: {submit.get('role', 'AXButton')}, "
                f"Strategy: {submit.get('strategy', 'mouse_click')}, "
                f"Post-delay: {submit.get('post_delay', 2.0)}s"
            )

        # Wrong answer behavior
        wrong = screen_info.get("wrong_answer_behavior") or screen_info.get("wrong_answer_signal")
        if wrong:
            parts.append(f"=== WRONG ANSWER BEHAVIOR ===\n{wrong}")

        # Completion mechanism (for ARTICLE)
        completion = screen_info.get("completion_mechanism")
        if completion:
            parts.append(f"=== COMPLETION MECHANISM ===\n{completion}")

        # Video states (for VIDEO)
        states = screen_info.get("states")
        if states:
            state_text = "\n".join(
                f"  {name}: signal='{s.get('tree_signal', '')}' → action='{s.get('action', '')}'"
                for name, s in states.items()
            )
            parts.append(f"=== VIDEO STATES ===\n{state_text}")

        # Navigation completion indicators
        indicators = screen_info.get("completion_indicators")
        if indicators:
            parts.append(
                f"=== COMPLETION INDICATORS ===\n"
                f"Done: {', '.join(indicators.get('done', []))}\n"
                f"Not done: {', '.join(indicators.get('not_done', []))}"
            )

    # 5. Learned observations (most recent = highest relevance)
    summary = learned.get("latest_summary", {})
    if summary:
        learned_parts = []
        patterns = summary.get("successful_patterns", [])
        if patterns:
            learned_parts.append(
                "Successful patterns from previous runs:\n" +
                "\n".join(f"  - {p}" for p in patterns)
            )
        failures = summary.get("known_failures", [])
        if failures:
            learned_parts.append(
                "Known failures (DO NOT repeat these approaches):\n" +
                "\n".join(f"  - {f}" for f in failures)
            )
        button_variants = summary.get("submit_button_variants", [])
        if button_variants:
            learned_parts.append(
                f"Submit button text seen on this platform: {', '.join(button_variants)}"
            )
        if learned_parts:
            parts.append(
                f"=== LEARNED FROM PREVIOUS RUNS ({summary.get('total_observations', 0)} observations) ===\n" +
                "\n".join(learned_parts)
            )

    return "\n\n".join(parts) if parts else ""


def _build_response_format(knowledge: dict, screen_type: str) -> str:
    """
    Build response format with screen-type-specific extraction hints.
    Appends to standard _BT_BUILDER_RESPONSE.
    """
    screen_info = knowledge.get("screen_types", {}).get(screen_type, {})
    extraction = screen_info.get("extraction")

    extraction_hint = ""
    if extraction:
        extraction_hint = f"""

=== EXTRACTION HINT (from platform knowledge) ===
Use this as a starting point for the "extract" field in your response.
Verify these container names exist in the actual accessibility tree above.
If they don't match, find the correct parent container and adjust.

Suggested extraction config:
{json.dumps(extraction, indent=2)}"""

    elif extraction is None:
        extraction_hint = """

=== EXTRACTION NOTE ===
This screen type typically has no unique educational content to extract.
Set "extract" to null in your response UNLESS you see content worth capturing."""

    return _BT_BUILDER_RESPONSE + extraction_hint


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
        analyze_tree, SCREEN_PATTERNS,
        get_handler_docs, get_question_type_docs,
        SECTION_5_HANDLERS_ORIGINAL, SECTION_6_QUESTION_TYPES_ORIGINAL,
        SECTION_7_STRATEGIES,
    )
    from spark.tasks.knowledge_loader import (
        load_knowledge, load_learned,
        get_handlers_for_screen, get_quirks_for_screen,
        get_question_types_for_screen,
    )

    tags = analyze_tree(tree)
    knowledge = load_knowledge(platform)

    # Explicit branch — knowledge vs. fallback
    use_knowledge = bool(knowledge and knowledge.get("screen_types"))

    logger.info(
        f"build_bt_from_tree: platform={platform} type={screen_type} "
        f"tags={tags} knowledge={'JIT' if use_knowledge else 'FALLBACK'} "
        f"has_guidance={'yes' if user_guidance else 'no'}"
    )

    # Build screen-specific guidance from detected tree signals (UNCHANGED)
    screen_guidance_parts = []
    for tag in tags:
        if tag in SCREEN_PATTERNS:
            screen_guidance_parts.append(SCREEN_PATTERNS[tag])

    screen_specific_guidance = "\n\n".join(screen_guidance_parts) if screen_guidance_parts else \
        "No specific assessment signals detected in the tree. Use the screenshot and tree to determine the right approach."

    # Assemble the prompt
    prompt_parts = [
        _BT_BUILDER_PROMPT.format(
            platform=platform,
            screen_type=screen_type,
            screen_specific_guidance=screen_specific_guidance,
        ),
    ]

    if use_knowledge:
        # JIT PATH: Knowledge-driven assembly
        learned = load_learned(platform, screen_type)
        handler_names = get_handlers_for_screen(knowledge, screen_type, tags)
        question_types = get_question_types_for_screen(knowledge, screen_type, tags)
        quirks = get_quirks_for_screen(knowledge, screen_type)

        # Knowledge context block
        knowledge_ctx = _build_knowledge_context(knowledge, screen_type, quirks, learned)
        if knowledge_ctx:
            prompt_parts.append(knowledge_ctx)

        # Selective handler docs
        prompt_parts.append(get_handler_docs(handler_names))
        prompt_parts.append(get_question_type_docs(question_types))
        prompt_parts.append(SECTION_7_STRATEGIES)

    else:
        # FALLBACK PATH: Exact current behavior
        prompt_parts.append(SECTION_5_HANDLERS_ORIGINAL)
        prompt_parts.append(SECTION_6_QUESTION_TYPES_ORIGINAL)
        prompt_parts.append(SECTION_7_STRATEGIES)

    # User guidance (UNCHANGED)
    if user_guidance:
        prompt_parts.append(
            f"=== USER GUIDANCE ===\n"
            f"The user has told you exactly what to do on this screen:\n"
            f'"{user_guidance}"\n\n'
            f"Build a BT that follows these instructions precisely.\n"
            f"Use the accessibility tree to find the exact element names and roles.\n"
            f"The user's guidance overrides the screen type classification if they conflict."
        )

    # Failed BT context (UNCHANGED)
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

    # Pruned tree for prompt (strip coordinates, element_id, redundant fields)
    pruned = prune_tree_for_prompt(tree)
    tree_json = json.dumps(pruned, indent=None, ensure_ascii=False)
    prompt_parts.append(f"=== ACCESSIBILITY TREE ===\n{tree_json}")

    # Response format
    if use_knowledge:
        prompt_parts.append(_build_response_format(knowledge, screen_type))
    else:
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

    # Extract: use Gemini BT builder's extract if provided.
    # Otherwise, make a separate focused Gemini call for extraction.
    extract = result.get("extract")
    if not extract and _should_extract(bt_screen_type) and screenshot_b64:
        extract = build_extract_config(tree, screenshot_b64, platform, bt_screen_type)

    return {
        "tree": bt,
        "screen_type": bt_screen_type,
        "extract": extract,
        "expected_next": result.get("expected_next", []),
    }
