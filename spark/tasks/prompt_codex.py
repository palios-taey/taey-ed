# 2026-02-20: Fixed $var.items → $var in BT examples (find_all returns list directly)
"""
Prompt Codex — Comprehensive self-contained consultation prompt builder.

Compiles the consultation prompt for the BT-builder agent
and classify_archetype.py (182 lines, simple heuristic).

Philosophy: Include EVERYTHING the consultation agent needs in one prompt.
~40K characters is ~5% of a 200K context window. No external file reads needed.

The prompt has 9 sections:
  1. Identity & Cardinal Rules (always)
  2. Files to Read (always)
  3. Screen Patterns for detected type (tree-driven)
  4. Platform Knowledge from knowledge.json (JIT) or full docs (fallback)
  5. Complete Handler Reference (always)
  6. LLM Question Types Reference (always)
  7. Click Strategies & Timing (always)
  8. Response Format & Rules (always)
  9. Reconsultation Context (only if spark_attempts > 0)
"""

import logging
import os
from collections import Counter
from pathlib import Path

logger = logging.getLogger("taey-ed")


# =========================================================================
# analyze_tree — Detect screen signals from accessibility tree
# =========================================================================

# Roles to skip (browser chrome, not web content)
_CHROME_ROLES = {"AXMenuBar", "AXMenuBarItem", "AXMenu", "AXToolbar"}

# Video detection keywords
_VIDEO_KEYWORDS = {
    "video player", "youtube", "play video", "vimeo player", "wistia",
    "pause video", "video playback", "playback speed", "video progress",
    "media player", "video timeline",
}


def _find_web_area(node: dict) -> dict:
    """Find AXWebArea subtree (web content, excluding browser chrome)."""
    if not isinstance(node, dict):
        return node
    if node.get("role") == "AXWebArea":
        return node
    for child in node.get("children", []):
        result = _find_web_area(child)
        if isinstance(result, dict) and result.get("role") == "AXWebArea":
            return result
    return node


def _count_roles(node: dict) -> Counter:
    """Count all AX roles in the tree, skipping chrome roles."""
    counts = Counter()

    def walk(n):
        if not isinstance(n, dict):
            return
        role = n.get("role", "")
        if role and role not in _CHROME_ROLES:
            counts[role] += 1
        for child in n.get("children", []):
            walk(child)

    walk(node)
    return counts


def _has_video_signals(node: dict) -> bool:
    """Check for video player keywords in tree text."""
    if not isinstance(node, dict):
        return False
    for field in ("name", "title", "value", "description"):
        text = node.get(field, "")
        if text and isinstance(text, str):
            lower = text.lower()
            if any(kw in lower for kw in _VIDEO_KEYWORDS):
                return True
    for child in node.get("children", []):
        if _has_video_signals(child):
            return True
    return False


# _has_post_answer_signals REMOVED (V20): This function hardcoded keyword
# detection ("next question", "show summary", "keep going") that would
# short-circuit analyze_tree() and force TRANSITION classification regardless
# of what else was on screen. Gemini handles this correctly via classification.


def analyze_tree(tree: dict) -> list:
    """
    Analyze accessibility tree and return detected PRESENCE tags.

    V20: No count thresholds. Detects WHAT element types are present,
    not HOW MANY. A true/false question with 2 radio buttons is just as
    much an exercise as one with 5 radio buttons. Gemini classifies;
    this function just reports what's on screen.

    Returns list of tags like ["HAS_RADIO", "HAS_TEXT_INPUT"].
    Multiple tags can be present for a single tree.

    Tags:
      HAS_RADIO      — Any AXRadioButton elements present
      HAS_CHECKBOX   — Any AXCheckBox elements present
      HAS_TEXT_INPUT  — Any AXTextArea or AXTextField present
      HAS_LINKS      — Any AXLink elements present
      HAS_VIDEO      — Video player detected (roles or keywords)
      HAS_COMBOBOX   — Any AXComboBox or AXPopUpButton present
      HAS_BUTTONS    — Any AXButton elements present
    """
    web_area = _find_web_area(tree)
    counts = _count_roles(web_area)
    tags = []

    # Video detection (keywords + role) — still checked first because
    # video players are unambiguous structural signals
    video_roles = counts.get("AXVideo", 0) + counts.get("AXMediaTimeline", 0)
    if video_roles > 0 or _has_video_signals(web_area):
        tags.append("HAS_VIDEO")

    # Presence checks — ANY count > 0, no thresholds
    if counts.get("AXRadioButton", 0) > 0:
        tags.append("HAS_RADIO")
    if counts.get("AXCheckBox", 0) > 0:
        tags.append("HAS_CHECKBOX")
    if counts.get("AXTextArea", 0) + counts.get("AXTextField", 0) > 0:
        tags.append("HAS_TEXT_INPUT")
    if counts.get("AXComboBox", 0) + counts.get("AXPopUpButton", 0) > 0:
        tags.append("HAS_COMBOBOX")
    if counts.get("AXLink", 0) > 0:
        tags.append("HAS_LINKS")
    if counts.get("AXButton", 0) > 0:
        tags.append("HAS_BUTTONS")

    return tags


# =========================================================================
# Section String Constants
# =========================================================================

SECTION_1_IDENTITY = """\
=== YOUR ROLE ===
You are building a behavior tree (BT) for an educational platform screen.
For DETERMINISTIC types (VIDEO, ARTICLE), this BT is stored in JSON signature files
and reused on every future encounter -- zero LLM cost. For DYNAMIC types (EXERCISE,
NAVIGATION, TRANSITION), the screen type is stored for recognition but the BT is
rebuilt fresh by Gemini each time because the content changes.

CONSULTATION: {consultation_id}
PLATFORM: {platform}
ESCALATION LEVEL: {escalation_level} (attempt {spark_attempts})

=== CARDINAL RULES ===
1. FALLBACK NODES ARE SUPPORTED. Use type: "fallback" for try-or-skip patterns
   (try first child, if it fails try next). Use type: "sequence" for must-all-succeed.
2. Execution uses TITLE, DESCRIPTION, and ROLE to find elements, NEVER element_id.
   Element IDs in tree.json are for YOUR visual reference only.
3. NEVER target "Skip" buttons. Exercises must be SOLVED or ESCALATED.
4. "Up next" rule on Khan is CONTEXTUAL, not absolute:
     - ALLOWED on TRANSITION_* and *_COMPLETE / *_CORRECT screens: "Up next: exercise"
       (or "Up next: video" / "Up next: article") is the canonical advance after
       finishing the prior unit. Equivalent to picking the next item from the sidebar.
     - FORBIDDEN mid-VIDEO (skips remaining content) and on NAVIGATION screens where
       the next item should be the next NON-COMPLETED sidebar lesson (Up next might
       jump past unfinished prerequisites — mastery-adaptive skip).
     Heuristic: if the screen type indicates a completion/transition state, "Up next"
     is the right click. Otherwise prefer explicit sidebar navigation.
5. NEVER put a screen in its own expected_next (creates silent infinite loops).
6. video_poll must be the ONLY action in its tree. No other children.
   Pipeline re-match loop handles screen transitions after video completes.
7. ONE attempt at wrong answers. Wrong answer = escalation, not retry.
8. Complete BEFORE navigate: answer → submit → wait → next.
9. For deterministic types (VIDEO, ARTICLE), the BT is stored with the screen
   signature and reused. For dynamic types, the BT is rebuilt each time.
   Always build a complete, correct tree regardless.
10. If you don't know what to do, respond with escalation rather than guessing.
    A wrong tree wastes more time than an honest "I don't know."
11. SCREEN-SHAPE BINDS question_type. Do not override based on perceived problem
    difficulty. The DETECTED tag in your prompt determines the call shape:
      HAS_TEXT_INPUT  → question_type="solve"          (LLM has the screenshot, can compute)
      HAS_RADIO       → question_type="solve_choice"   (or solve_choice + has_text_field combo)
      HAS_CHECKBOX    → question_type="solve_checkbox"
      HAS_COMBOBOX    → question_type="solve_choice"   with combobox handlers
      HAS_MANY_LINKS  → question_type="navigate"
    solve_complex is RESERVED for screens whose answer requires reading visual
    elements NOT in the AX tree (chart data, image content). A math word problem
    on a text-input screen is NOT solve_complex — solve already gets the screenshot.
    Routine "this looks computational" promotion to solve_complex is forbidden.
12. RESPONSE-KEY CONTRACT (uniform across question_types):
      solve / solve_choice / solve_complex / navigate → read $llm.answer (string)
      solve_checkbox                                    → read $llm.selected (list)
      solve_matching                                    → read $llm.matches (dict)
13. UNIVERSAL IMAGE/HIDDEN-CONTENT EXTRACTION (all platforms): when the answer
    depends on content embedded in an image, dropdown, modal, or any element
    NOT directly readable from the AX tree as text, the BT MUST do a two-step
    extraction BEFORE asking the LLM to choose an answer:
      Step A (extract): open / OCR / enumerate the hidden content into the BT's
        blackboard. Examples: for dropdowns, click each popup and
        discover_menu the options; for image-based questions, send_to_llm with
        question_type='solve' asking the LLM to DESCRIBE each option's
        diagram/text first (return structured text).
      Step B (reason): a second send_to_llm call uses the extracted structured
        data as `context` and asks for the actual answer.
    If the AX tree already contains the full question + option text and no
    image content is essential to picking the answer, single-step is fine —
    do NOT add unnecessary two-step overhead. This rule fires when the
    option text is degenerate (e.g., "(Choice A) A box with arrows"), when
    diagrams carry numeric values, or when the screen has hidden menus.
14. RESPONSE FORMAT IS JSON ONLY. Emit a single JSON object as your final
    output — no prose before or after. The output_schema is defined per
    consult below.
      solve_matching                                    → read $llm.matches (dict)
    Always read the field documented for the question_type you used. No exceptions.

=== WHAT NOT TO DO (Anti-Patterns from V4-V9) ===
- Use fallback nodes for OPTIONAL steps only (e.g., try Mark Complete, skip if absent)
- NEVER retry same failing tree — different approach each attempt, max 3 total
- NEVER use confidence thresholds — signature matching handles routing
- NEVER hardcode lesson/unit names in targets — use $nav.answer from LLM
- NEVER use poll_interval param on video_poll — handler ignores it, sleeps 30s
- NEVER auto-click "Try again" on wrong answers — creates bot detection risk
- NEVER use `duration` param on wait handler — the param is `seconds`
- NEVER put for_each/conditional params under `params:` — top-level keys only
- NEVER use discover_menu on ARIA comboboxes — AXMenu doesn't exist for them
- NEVER compose raw click + press_key arrows for ARIA comboboxes — the
  React-portaled options live outside the keyboard handler's wrapper.
  Use select_dropdown_option, which owns focus_press + verification."""


def build_section_2(consultation_id: str, is_reconsultation: bool,
                    context: dict) -> str:
    """Build Section 2: Files to Read."""
    section = f"""\
=== STEP 1: READ THESE FILES (in this order) ===

1. SCREENSHOT: /tmp/taey-ed-consult/{consultation_id}/screenshot.png
   READ THIS FIRST. You CAN view images. Your tree must match what you SEE.
   Look at: what page is this? What buttons are visible? What content is shown?

2. TREE: /tmp/taey-ed-consult/{consultation_id}/tree.json
   Focus on the AXWebArea subtree — skip browser chrome (AXMenuBar, AXToolbar).
   Look for: AXRadioButton, AXCheckBox, AXTextArea, AXTextField, AXComboBox,
   AXLink, AXButton, AXVideo, AXImage.
   Count them. The counts tell you what type of screen this is.

3. METADATA: /tmp/taey-ed-consult/{consultation_id}/metadata.json
   Contains: platform, escalation_level, spark_attempts, context."""

    if is_reconsultation:
        failure_reason = context.get("failure_reason", "unknown")
        section += f"""

4. BT DEBUG LOG: /tmp/taey-ed-consult/{consultation_id}/bt_debug.log
   Shows what the PREVIOUS tree tried and exactly where it failed.
   DO NOT output the same tree. Change targeting strategy, click strategy,
   or screen classification based on what you learn from this log.
   Previous failure reason: {failure_reason}"""

    return section


# =========================================================================
# Screen Pattern Constants — MIGRATED 2026-05-19
# =========================================================================
#
# The PATTERN_HAS_* constants and SCREEN_PATTERNS dict that used to live
# here have been migrated into knowledge.json operational_notes per
# subtype. The loader injects them via get_operational_notes_for_screen.
# Mapping:
#   PATTERN_HAS_RADIO     -> EXERCISE.multiple_choice  (aliases: radio)
#   PATTERN_HAS_CHECKBOX  -> EXERCISE.multiple_select  (aliases: checkbox)
#   PATTERN_HAS_TEXT_INPUT-> EXERCISE.numeric_input + expression_input + free_response
#   PATTERN_HAS_LINKS     -> NAVIGATION (category-level)
#   PATTERN_HAS_VIDEO     -> VIDEO (category-level)
#   PATTERN_HAS_COMBOBOX  -> EXERCISE.dropdown        (aliases: combobox)
#   PATTERN_TRANSITION    -> TRANSITION (category-level)
#
# knowledge.json is now the SINGLE source of truth for screen-specific
# BT patterns. To update a pattern, edit the matching subtype operational_note.

SECTION_5_HANDLERS = """\
=== HANDLER REFERENCE (18 registered + 2 composable) ===

REGISTERED HANDLERS — Use as action: value in BT nodes.
Any other action name will SILENTLY FAIL (logs error, returns FAILURE).

find_and_click:
  Purpose: Find element by text/role, then click it
  Params:
    target (str, required): Text to search for in element name/description
    role (str, optional): AX role filter (AXButton, AXLink, AXRadioButton, etc.)
    match_mode (str): "exact" or "contains" (default: exact)
    strategy (str): "mouse_click" (ALWAYS for browser content incl. radio/checkbox), "focus_space" (NATIVE Mac apps only — silently no-ops on Chrome web widgets),
                    "focus_enter" (buttons), "ax_press" (native Mac apps)
    fallback_roles (list): Alternate roles to try if primary role not found
    post_delay (float): Seconds to wait after click (default: 0)
  Returns: {success: true/false, element: {...}}
  4-tier fallback: exact→contains→alternate roles→no role filter

find_and_type:
  Purpose: Find text field, optionally focus via click, then type text
  Params:
    target (str): Text to search for in field name (can be "" for first match)
    text (str, required): Text to type into the field
    role (str): AXTextArea, AXTextField, etc.
    focus_strategy (str): How to focus before typing
  Returns: {success: true/false}

find_all:
  Purpose: Find ALL elements matching role/filter. Enriches each with labels
  Params:
    role (str): AX role to search for
    description_contains (str, optional): Filter by description substring
  Returns: list of [{element, description, popup_desc, label}, ...]
  The return value IS the list directly (not wrapped in {success, items}).
  Use "$var" to reference the list, NOT "$var.items".
  Labels: Preceding text found via _find_preceding_label() in bt_helpers.py
  Enrichment includes completion indicators ("Completed", "Not started", etc.)

click:
  Purpose: Click element from blackboard variable
  Params:
    element (ref): Blackboard reference to element dict (e.g., "$_current")
    target (str): Alternative to element — text to find
    role (str): Role filter
    match_mode (str): "exact" or "contains"
    strategy (str): Click strategy
  Note: If element is dict from find_all, re-finds fresh by description

extract_question:
  Purpose: Parse question text + options from accessibility tree
  Params (all optional — look at the tree to determine appropriate values):
    question (dict): Criteria for finding the question/prompt text
      - role (str): AX role to match (default: "AXStaticText")
      - parent_contains (str): Match only if parent name contains this string
      - contains (str): Match only if text contains this substring
      - min_length (int): Minimum text length to qualify
    options (dict): Criteria for finding answer option elements
      - role (str): AX role (e.g., "AXRadioButton", "AXCheckBox", "AXButton")
      - exclude_titles (list[str]): Button names to skip (e.g., ["Back", "Menu"])
    text (list[dict]): Reference/context text criteria (same format as extract text criteria)
  Returns: {question_text: str, options: [str], reference_texts: [str], question_type: str}
  Scopes to AXWebArea, skips browser chrome
  IMPORTANT: You MUST provide params. Examine the tree to find the parent container
  names and roles where the question/prompt content lives.

send_to_llm:
  Purpose: Call Spark /api/v1/generate for AI-powered decisions
  Params:
    question (str): The question text
    question_type (str): "solve_choice", "solve_checkbox", "solve", "solve_matching",
                         "solve_assessment", "solve_complex", "navigate"
    options (list): For solve_choice/checkbox
    items (list): For navigate (from find_all)
    context (str): Additional context from KB
    image_descriptions (list): From VLM
    has_text_field (bool): For choice + text response variant
  Returns: Varies by question_type (see Section 6 below)

video_poll:
  Purpose: Poll for video completion
  Params: NONE. Handler ignores ALL params. Sleeps 30 seconds (HARDCODED).
  Returns: {success: true, continue_loop: true} ALWAYS
  CRITICAL: Must be the ONLY action in its tree. sequence runs ALL children
  before checking continue_loop.

wait:
  Purpose: Sleep for specified duration
  Params:
    seconds (float): Duration. NOT "duration" — code reads params.get("seconds", 1.0)

press_key:
  Purpose: Send keyboard event via Quartz CGEvent
  Params:
    key (str): "return", "enter", "tab", "escape", "space", "backspace",
               "delete", "up", "down", "left", "right", "home", "end",
               "pageup", "pagedown"
    modifiers (list): ["shift", "cmd", "alt", "ctrl"]

scroll:
  Purpose: Scroll via Quartz scroll wheel event
  Params:
    direction (str): "up", "down", "left", "right"
    amount (int): Lines to scroll (default: 3)

wait_for_element:
  Purpose: Poll until element appears in tree
  Params:
    target (str): Element text to search for
    role (str): AX role filter
    max_wait (float): Maximum seconds to wait (default: 60)
  Checks every 2 seconds

discover_menu:
  Purpose: Capture tree, extract all menu items (scopes to AXWebArea)
  Params:
    role (str): Default "AXMenuItem"
  Filters out system menus (Apple, Window, Help)
  NOTE: Only works for native menus. FAILS on ARIA comboboxes.

lookup_match:
  Purpose: Dictionary lookup with partial matching fallback
  Params:
    matches (dict): Key-value mapping
    key (str): Key to look up
  Bidirectional case-insensitive substring check

store_qa:
  Purpose: Store Q&A pair in SQLite
  Params (ALL EXPLICIT — not automatic):
    question (str, required)
    answer (str, required)
    question_type (str, required)
  platform and course_id come from ExecutionContext (automatic)

solve_assessment_page:
  Purpose: Full multi-question assessment orchestration
  Params: None (orchestrates internally)
  Captures tree, finds question containers, iterates each, calls Spark LLM

press_escape:
  Purpose: Send Escape key
  Params: None

drag:
  Purpose: Synthesized mouse drag (mousedown → hold → stepped moves → drop).
    For mouse-event drag widgets ONLY (e.g. Perseus Sortable / matcher).
  Params:
    start (dict, required): {"x": <num>, "y": <num>} — drag origin center
    end (dict, required): {"x": <num>, "y": <num>} — drop target center
    steps (int): intermediate moves (default 18)
    post_delay (float): seconds after mouseup (default 0)
  SHAPE WARNING: start/end are NESTED dicts. Flat keys (start_x, from_x,
    to_y, ...) are silently invalid — the action returns None and the BT fails.

type_keys:
  Purpose: Type arbitrary Unicode (math symbols, Greek, subscripts) into the
    FOCUSED element. Caller must focus the target first (click/find_and_click).
  Params:
    text (str, required): The literal text to type.
    post_delay (float): seconds after typing (default 0).

COMPOSABLE NODE TYPES — These are NOT handlers. Use as action: value.

for_each:
  CRITICAL: Parameters go at TOP LEVEL of the node, NOT inside params:
  {
    "type": "action",
    "action": "for_each",
    "items": "$all_lessons",
    "variable": "lesson",
    "do": {
      "type": "action",
      "action": "find_and_click",
      "params": {"target": "$lesson.description"}
    }
  }
  WRONG: putting items/variable/do inside params: (silently reads None)
  Sets $_current and $_index during iteration.
  store_to_current: writes into $_current dict.

conditional:
  CRITICAL: Parameters go at TOP LEVEL:
  {
    "type": "action",
    "action": "conditional",
    "condition": "$has_text",
    "then": {...},
    "else": {...}
  }
  String "false" is normalized to boolean False before truthiness check.

BLACKBOARD VARIABLE SUBSTITUTION:
- $var → blackboard["var"]
- $var.field → blackboard["var"]["field"]
- $var.0 → blackboard["var"][0] (numeric = list index)
- $var.field.nested → deep access (any combination)
- Non-$ strings → returned as-is
- Lists: ["text", "$var"] → each element resolved independently
- store: "key" on any action node saves return dict to blackboard["key"]
- If a store: action fails (returns None), the key is NEVER written.
  Later $key.field resolves to None. PLAN FOR THIS."""

# Keep original for byte-for-byte fallback when knowledge.json absent
SECTION_5_HANDLERS_ORIGINAL = SECTION_5_HANDLERS

# Individual handler documentation blocks for JIT assembly
HANDLER_DOCS = {
    "drag": """\
drag:
  Purpose: Synthesized mouse drag (mousedown → hold → stepped moves → drop).
    For mouse-event drag widgets ONLY (e.g. Perseus Sortable / matcher).
    Does NOT fire HTML5-native dragstart/dragover, and does NOT work on
    PointerEvent widgets (Mafs interactive-graph — use the keyboard path).
  Params:
    start (dict, required): {"x": <num>, "y": <num>} — drag origin center
      (POINTS, same space as visible_bbox; compute bbox center like click_at).
    end (dict, required): {"x": <num>, "y": <num>} — drop target center.
    steps (int): intermediate moves (default 18).
    press_hold (float): seconds held down BEFORE the first move (default 0.08).
    step_delay (float): seconds between moves (default 0.02).
    release_hold (float): seconds held at target before mouseup (default 0.05).
    post_delay (float): seconds after mouseup (default 0).
  ACTIVATION (Khan/Perseus Sortable, diagnosed 2026-06-11): the DEFAULT 80ms
    press_hold is TOO SHORT — the drag library needs a long-press to enter
    drag state (mousedown alone reads as a click; widget never engages and
    Check stays disabled). ALWAYS set press_hold: 0.25 and release_hold: 0.15
    on Sortable widgets (matcher/ranking/sorter). If that fails to engage:
    press_hold: 0.40; still nothing: steps: 8 with step_delay: 0.04 (bigger
    first move crosses 5-15px tolerance thresholds).
  SHAPE WARNING: start/end are NESTED dicts. Flat keys (start_x, from_x,
    to_y, ...) are silently invalid — the action returns None, BT fails.
  Returns: {success: true/false}
  Verify after EACH drop by re-reading the tree/screenshot — drops can miss.""",

    "type_keys": """\
type_keys:
  Purpose: Type arbitrary Unicode (math symbols, Greek, subscripts) into the
    FOCUSED element without a keymap. Caller must focus the target first.
  Params:
    text (str, required): The literal text to type.
    post_delay (float): seconds after typing (default 0).""",

    "find_and_click": """\
find_and_click:
  Purpose: Find element by EXACT name and click it. Use ONLY for elements
    whose visible text is stable across visits (e.g., "Check", "Submit",
    "Continue", "Try again", "Replay Video", "Cancel"). For elements whose
    text varies between visits (e.g., "Up next: <video-title>", lesson
    items with course-specific titles, anything with dynamic content),
    use click_at instead — read the element's visible_bbox from the AX
    tree at decision time and click by coordinates. Exact-match only;
    no guessing.
  Params:
    target (str, required): Exact name of the element (must literally match).
    role (str, required): AX role filter (AXButton, AXLink, AXRadioButton, ...).
    match_mode (str): MUST be "exact" — "contains" is FORBIDDEN per the
      no-guessing rule. If exact match doesn't fit, switch to click_at.
    strategy (str): "mouse_click" (ALWAYS for browser content incl. radio/checkbox), "focus_space" (NATIVE Mac apps only — silently no-ops on Chrome web widgets),
                    "focus_enter" (buttons), "ax_press" (native Mac only — Chrome ignores).
    post_delay (float): Seconds to wait after click (default: 0).
  Returns: {success: true/false, element: {...}}
  Failure mode: returns success=false if no element matches target+role exactly.
  When that happens, switch the next BT to click_at — DO NOT relax match_mode.""",

    "click_at": """\
click_at:
  Purpose: Click at exact pixel coordinates. THIS IS THE AI-FIRST PATH for
    any element whose visible text varies between visits. Read the target
    element's visible_bbox from the AX tree at decision time, compute the
    bbox center, and click there. No name-matching, no guessing.
  Params:
    x (number, required): Window-relative x coordinate (bbox_x + bbox_width/2).
    y (number, required): Window-relative y coordinate (bbox_y + bbox_height/2).
    post_delay (float): Seconds to wait after click (default: 0).
  Returns: {success: true/false}
  When to use:
    - 'Up next: <video-title>' AXLink on post-video screens (title varies per video)
    - Sidebar lesson items with course-specific titles
    - Any element you can SEE in the screenshot but whose name varies
    - When find_and_click returned success=false on a previous attempt
  How to use:
    1. Look at the screenshot. Identify the target visually.
    2. Find that element in the AX tree (by role + partial text + position).
    3. Read its visible_bbox: [x, y, width, height].
    4. Output: {"action": "click_at", "params": {"x": x+width/2, "y": y+height/2,
       "post_delay": <appropriate timing>}}
  Coordinates are in POINTS (not pixels — no Retina scale factor math required).""",

    "find_and_type": """\
find_and_type:
  Purpose: Find text field, optionally focus via click, then type text
  Params:
    target (str): Text to search for in field name (can be "" for first match)
    text (str, required): Text to type into the field
    role (str): AXTextArea, AXTextField, etc.
    focus_strategy (str): How to focus before typing
  Returns: {success: true/false}""",

    "find_all": """\
find_all:
  Purpose: Find ALL elements matching role/filter. Enriches each with labels
  Params:
    role (str): AX role to search for
    description_contains (str, optional): Filter by description substring
  Returns: list of [{element, description, popup_desc, label}, ...]
  The return value IS the list directly (not wrapped in {success, items}).
  Use "$var" to reference the list, NOT "$var.items".
  Labels: Preceding text found via _find_preceding_label() in bt_helpers.py
  Enrichment includes completion indicators ("Completed", "Not started", etc.)""",

    "click": """\
click:
  Purpose: Click element from blackboard variable (element ref from find_all).
  Params:
    element (ref, required): Blackboard reference to element dict
      (e.g., "$_current" inside for_each over find_all results).
    strategy (str): Click strategy ("mouse_click" default for browsers).
  Note: If element is a dict from find_all, the handler re-finds it fresh
    by description before clicking. For variable-text targets where you
    don't have a find_all ref, use click_at with bbox from the tree.
  match_mode: NOT a parameter here — "contains" is FORBIDDEN per the
    no-guessing rule. Use click_at if you need to click without an exact
    element ref.""",

    "extract_question": """\
extract_question:
  Purpose: Parse question text + options from accessibility tree
  Params (all optional — look at the tree to determine appropriate values):
    question (dict): Criteria for finding the question/prompt text
      - role (str): AX role to match (default: "AXStaticText")
      - parent_contains (str): Match only if parent name contains this string
      - contains (str): Match only if text contains this substring
      - min_length (int): Minimum text length to qualify
    options (dict): Criteria for finding answer option elements
      - role (str): AX role (e.g., "AXRadioButton", "AXCheckBox", "AXButton")
      - exclude_titles (list[str]): Button names to skip (e.g., ["Back", "Menu"])
    text (list[dict]): Reference/context text criteria (same format as extract text criteria)
  Returns: {question_text: str, options: [str], reference_texts: [str], question_type: str}
  Scopes to AXWebArea, skips browser chrome
  IMPORTANT: You MUST provide params. Examine the tree to find the parent container
  names and roles where the question/prompt content lives.""",

    "send_to_llm": """\
send_to_llm:
  Purpose: Call Spark /api/v1/generate for AI-powered decisions
  Params:
    question (str): The question text
    question_type (str): "solve_choice", "solve_checkbox", "solve", "solve_matching",
                         "solve_assessment", "solve_complex", "navigate"
    options (list): For solve_choice/checkbox
    items (list): For navigate (from find_all)
    context (str): Additional context from KB
    image_descriptions (list): From VLM
    has_text_field (bool): For choice + text response variant
  Returns: Varies by question_type (see LLM Question Types below)""",

    "video_poll": """\
video_poll:
  Purpose: Poll for video completion
  Params: NONE. Handler ignores ALL params. Sleeps 30 seconds (HARDCODED).
  Returns: {success: true, continue_loop: true} ALWAYS
  CRITICAL: Must be the ONLY action in its tree. sequence runs ALL children
  before checking continue_loop.""",

    "wait": """\
wait:
  Purpose: Sleep for specified duration
  Params:
    seconds (float): Duration. NOT "duration" — code reads params.get("seconds", 1.0)""",

    "press_key": """\
press_key:
  Purpose: Send keyboard event via Quartz CGEvent
  Params:
    key (str): "return", "enter", "tab", "escape", "space", "backspace",
               "delete", "up", "down", "left", "right", "home", "end",
               "pageup", "pagedown"
    modifiers (list): ["shift", "cmd", "alt", "ctrl"]""",

    "scroll": """\
scroll:
  Purpose: Scroll via Quartz scroll wheel event
  Params:
    direction (str): "up", "down", "left", "right"
    amount (int): Lines to scroll (default: 3)""",

    "wait_for_element": """\
wait_for_element:
  Purpose: Poll until element appears in tree
  Params:
    target (str): Element text to search for
    role (str): AX role filter
    max_wait (float): Maximum seconds to wait (default: 60)
  Checks every 2 seconds""",

    "discover_menu": """\
discover_menu:
  Purpose: Capture tree, extract all menu items (scopes to AXWebArea)
  Params:
    role (str): Default "AXMenuItem"
  Filters out system menus (Apple, Window, Help)
  NOTE: Only works for native menus. FAILS on ARIA comboboxes.""",

    "select_dropdown_option": """\
select_dropdown_option:
  Purpose: Semantic ARIA combobox/listbox selection with verification.
    Use this for ANY combobox or popup widget — Wonder Blocks SingleSelect,
    React Aria combobox, native AXPopUpButton. Do NOT compose raw click +
    press_key arrows for these.
  Params:
    trigger_element (AXUIElement, optional): Combobox AX element ref
      (preferred — pass via $popup.element from find_all + for_each)
    trigger_target (str, optional): Text to find combobox by, default
      "Select an answer"
    trigger_role (str): "AXComboBox" (default) or "AXPopUpButton"
    trigger_match_mode (str): "contains" (default) or "exact"
    option (str, REQUIRED): Option text to select (e.g. "Kr", "Carbon")
    open_strategy (str): Default "mouse_click"
    open_wait (float): Default 0.5
    verify_wait (float): Default 0.35
    strategies (list, optional): Override fallback ladder. Default
      ["focus_press", "focus_space", "focus_enter", "mouse_click",
      "ax_press"]
  Returns: {"success": bool, "strategy": str, "option": str} on success,
    or {"success": False, "error": str, "seen": [...], "errors": [...]}
    on failure.
  NOTES:
    - Activates Chrome before raw events (frontmost-routing safe)
    - Walks FULL app AX tree (Wonder Blocks portals options outside webarea)
    - Normalizes "<text> not selected" / "<text> selected" suffixes
    - Verifies trigger AXValue changed before returning success — no silent
      ACTION RETURNED NONE failures""",

    "lookup_match": """\
lookup_match:
  Purpose: Dictionary lookup with partial matching fallback
  Params:
    matches (dict): Key-value mapping
    key (str): Key to look up
  Bidirectional case-insensitive substring check""",

    "store_qa": """\
store_qa:
  Purpose: Store Q&A pair in SQLite
  Params (ALL EXPLICIT — not automatic):
    question (str, required)
    answer (str, required)
    question_type (str, required)
  platform and course_id come from ExecutionContext (automatic)""",

    "solve_assessment_page": """\
solve_assessment_page:
  Purpose: Full multi-question assessment orchestration
  Params: None (orchestrates internally)
  Captures tree, finds question containers, iterates each, calls Spark LLM""",

    "press_escape": """\
press_escape:
  Purpose: Send Escape key
  Params: None""",

    "for_each": """\
for_each:
  CRITICAL: Parameters go at TOP LEVEL of the node, NOT inside params:
  {
    "type": "action",
    "action": "for_each",
    "items": "$all_lessons",
    "variable": "lesson",
    "do": {
      "type": "action",
      "action": "find_and_click",
      "params": {"target": "$lesson.description"}
    }
  }
  WRONG: putting items/variable/do inside params: (silently reads None)
  Sets $_current and $_index during iteration.
  store_to_current: writes into $_current dict.""",

    "conditional": """\
conditional:
  CRITICAL: Parameters go at TOP LEVEL:
  {
    "type": "action",
    "action": "conditional",
    "condition": "$has_text",
    "then": {...},
    "else": {...}
  }
  String "false" is normalized to boolean False before truthiness check.""",
}

BLACKBOARD_DOCS = """\
BLACKBOARD VARIABLE SUBSTITUTION:
- $var → blackboard["var"]
- $var.field → blackboard["var"]["field"]
- $var.0 → blackboard["var"][0] (numeric = list index)
- $var.field.nested → deep access (any combination)
- Non-$ strings → returned as-is
- Lists: ["text", "$var"] → each element resolved independently
- store: "key" on any action node saves return dict to blackboard["key"]
- If a store: action fails (returns None), the key is NEVER written.
  Later $key.field resolves to None. PLAN FOR THIS."""


def get_handler_docs(handler_names: list) -> str:
    """
    Return handler documentation for only the specified handlers.
    Always includes blackboard docs and composable node types (for_each, conditional).

    If handler_names is empty, returns the FULL original SECTION_5_HANDLERS
    for backward compatibility.
    """
    if not handler_names:
        return SECTION_5_HANDLERS_ORIGINAL

    names = set(handler_names)
    names.add("for_each")
    names.add("conditional")

    docs = []
    for name in sorted(names):
        if name in HANDLER_DOCS:
            docs.append(HANDLER_DOCS[name])
        else:
            logger.warning(f"get_handler_docs: unknown handler '{name}'")

    header = f"=== HANDLER REFERENCE ({len(docs)} handlers for this screen) ===\n\n"
    header += "REGISTERED HANDLERS — Use as action: value in BT nodes.\n"
    header += "Any other action name will SILENTLY FAIL (logs error, returns FAILURE).\n\n"

    return header + "\n\n".join(docs) + "\n\n" + BLACKBOARD_DOCS


SECTION_6_QUESTION_TYPES = """\
=== LLM QUESTION TYPES (via send_to_llm → /api/v1/generate) ===

All types route through Gemini 2.5 Pro.

solve_choice:
  Input: question (str), options (list of str)
  Output: {success: true, answer: "exact option text from the list"}
  How: Maps options to A/B/C, asks LLM for letter, maps back via match_to_option()
  match_to_option() 5-stage fallback: bare letter → letter+punct → exact → substring → word overlap
  Timeout: 60s, Max tokens: 128

solve_choice (with has_text_field=True):
  Input: question (str), options (list), has_text_field=True
  Output: {answer: "option text", text_response: "reflection text"}
  How: Uses SOLVE_CHOICE_WITH_TEXT_PROMPT. Parsed by parse_choice_with_text()
  Timeout: 60s, Max tokens: 256

solve_checkbox:
  Input: question (str), options (list of str)
  Output: {success: true, selected: ["opt1", "opt3"]}
  How: Asks for comma-separated letters. Has own inline letter-to-option mapping.
  WARNING: 30-char truncation on fallback parsing (known bug — use full text)
  Timeout: 60s, Max tokens: 128

solve:
  Input: question (str)
  Output: {success: true, answer: "text answer"}
  How: Direct text generation for fill-in-blank, short answer
  Timeout: 60s, Max tokens: 128

solve_matching:
  Input: items (list of dicts with label, popup_desc, options)
  Output: {matches: {popup_desc: "option", label: "option"}} (dual-keyed)
  How: Numbered matching format. Parsed by parse_matching_response()
  Timeout: 60s, Max tokens: 128

solve_assessment:
  Input: items (list of dicts with type, question, options)
  Output: {answers: [{type, selected}]}
  How: Multi-question JSON format
  WARNING: Currently hardcoded to "ChatGPT for educators" domain context
  Timeout: 180s, Max tokens: 2048

solve_complex:
  Input: question (str) + screenshot (multimodal)
  Output: {success: true, answer: "answer text"}
  How: Sends screenshot to Gemini 2.5 Pro for visual question understanding
  Use when question has images/diagrams not captured in tree text

navigate:
  Input: items (list of dicts with label, description/popup_desc from find_all)
  Output: {success: true, answer: "description text of first incomplete item"}
  How: NAVIGATE_PROMPT is platform-agnostic. Looks for generic completion indicators:
    "Completed", "Mastery points", checkmarks, percentage scores,
    "Not started", empty labels, "Try again", "Practice"
  Works across platforms without modification.
  Accepts both popup_desc and description field names (backward compat).
  Timeout: 60s, Max tokens: 128

UNKNOWN question_type:
  Silently falls through to solve (text) behavior. A typo like "solve_chice"
  will generate a text answer instead of failing. ALWAYS double-check spelling."""

# Keep original for byte-for-byte fallback
SECTION_6_QUESTION_TYPES_ORIGINAL = SECTION_6_QUESTION_TYPES

# Individual question type documentation blocks for JIT assembly
QUESTION_TYPE_DOCS = {
    "solve_choice": """\
solve_choice:
  Input: question (str), options (list of str)
  Output: {success: true, answer: "exact option text from the list"}
  How: Maps options to A/B/C, asks LLM for letter, maps back via match_to_option()
  match_to_option() 5-stage fallback: bare letter → letter+punct → exact → substring → word overlap
  Timeout: 60s, Max tokens: 128

solve_choice (with has_text_field=True):
  Input: question (str), options (list), has_text_field=True
  Output: {answer: "option text", text_response: "reflection text"}
  How: Uses SOLVE_CHOICE_WITH_TEXT_PROMPT. Parsed by parse_choice_with_text()
  Timeout: 60s, Max tokens: 256""",

    "solve_checkbox": """\
solve_checkbox:
  Input: question (str), options (list of str)
  Output: {success: true, selected: ["opt1", "opt3"]}
  How: Asks for comma-separated letters. Has own inline letter-to-option mapping.
  WARNING: 30-char truncation on fallback parsing (known bug — use full text)
  Timeout: 60s, Max tokens: 128""",

    "solve": """\
solve:
  Input: question (str)
  Output: {success: true, answer: "text answer"}
  How: Direct text generation for fill-in-blank, short answer
  Timeout: 60s, Max tokens: 128""",

    "solve_matching": """\
solve_matching:
  Input: items (list of dicts with label, popup_desc, options)
  Output: {matches: {popup_desc: "option", label: "option"}} (dual-keyed)
  How: Numbered matching format. Parsed by parse_matching_response()
  Timeout: 60s, Max tokens: 128""",

    "solve_assessment": """\
solve_assessment:
  Input: items (list of dicts with type, question, options)
  Output: {answers: [{type, selected}]}
  How: Multi-question JSON format
  WARNING: Currently hardcoded to "ChatGPT for educators" domain context
  Timeout: 180s, Max tokens: 2048""",

    "solve_complex": """\
solve_complex:
  Input: question (str) + screenshot (multimodal)
  Output: {success: true, answer: "answer text"}
  How: Sends screenshot to Gemini 2.5 Pro for visual question understanding
  Use when question has images/diagrams not captured in tree text""",

    "navigate": """\
navigate:
  Input: items (list of dicts with label, description/popup_desc from find_all)
  Output: {success: true, answer: "description text of first incomplete item"}
  How: NAVIGATE_PROMPT is platform-agnostic. Looks for generic completion indicators:
    "Completed", "Mastery points", checkmarks, percentage scores,
    "Not started", empty labels, "Try again", "Practice"
  Works across platforms without modification.
  Accepts both popup_desc and description field names (backward compat).
  Timeout: 60s, Max tokens: 128""",
}


def get_question_type_docs(type_names: list) -> str:
    """
    Return question type docs for only the specified types.
    Empty list = full original docs (fallback).
    """
    if not type_names:
        return SECTION_6_QUESTION_TYPES_ORIGINAL

    docs = [QUESTION_TYPE_DOCS[t] for t in type_names if t in QUESTION_TYPE_DOCS]

    header = f"=== LLM QUESTION TYPES ({len(docs)} types for this screen) ===\n\n"
    header += "All types route through Gemini 2.5 Pro.\n\n"

    unknown_note = ("\n\nUNKNOWN question_type:\n"
                    "  Silently falls through to solve (text) behavior. "
                    "ALWAYS double-check spelling.")

    return header + "\n\n".join(docs) + unknown_note


SECTION_7_STRATEGIES = """\
=== CLICK STRATEGIES ===

| Strategy     | Use When                          | How It Works                    |
|-------------|-----------------------------------|----------------------------------|
| mouse_click | Browser elements (DEFAULT, safest) | CGEvent mouse at element center |
| focus_space | Radio buttons, checkboxes         | Focus element → press Space      |
| focus_enter | Standard browser buttons          | Focus element → press Enter      |
| focus_press | ARIA listbox options, portaled    | AX focus → AXPress (VoiceOver path) |
| ax_press    | Native Mac apps (JavaFX, Cocoa)   | AXPress accessibility action     |

DEFAULT: mouse_click for all browser platforms (Khan Academy, Coursera, etc.)
EXCEPTION: Radio/checkbox use focus_space (mouse_click doesn't reliably toggle)
EXCEPTION: ARIA combobox/listbox (Wonder Blocks SingleSelect, React Aria
  combobox, etc.) — do NOT click options or arrow-key navigate. Use the
  semantic select_dropdown_option handler (see HAS_COMBOBOX), which uses
  the focus_press strategy under the hood.

=== AI-FIRST CLICK PROTOCOL ===

RULE: No contains matching. No fuzzy matching. Every click action is
either find_and_click with match_mode="exact", or click_at with x/y
coordinates derived from the AX tree's visible_bbox.

Decision table:

| Element name stable across visits? | Action |
|-----------------------------------|--------|
| Yes (Check, Continue, Try again, Replay Video, Play video) | find_and_click target="<exact>" role="<AX role>" match_mode="exact" |
| No — name varies (Up next: <title>, lesson links) | click_at x=<bbox_cx> y=<bbox_cy> |
| Visible in screenshot, name unstable or absent | click_at x=<bbox_cx> y=<bbox_cy> |

click_at bbox derivation: locate the target element node in the AX tree;
its visible_bbox = [x, y, width, height] gives bbox_cx = x + width/2,
bbox_cy = y + height/2. These are the click_at parameters. No prose
reasoning in the output — the output is JSON only (see YOUR RESPONSE
section). Reason internally; emit JSON.

find_and_click failure handling: if it returns success=false, do NOT
relax match_mode. Use click_at with the bbox of the visually-correct
element instead.

The Mac handler is exact-only. The legacy 4-tier fallback
(exact→contains→alt roles→no role) is removed.

=== TIMING (post_delay values) ===

| Context                                    | post_delay |
|-------------------------------------------|------------|
| Page-changing clicks (Next, Continue)      | 3.0-4.0s   |
| Answer selection (radio/checkbox)          | 1.0s        |
| Submit/Check button                       | 2.0-3.0s    |
| Text field focus before typing            | 0.3s        |
| SPA navigation (course/unit clicks)       | 3.0s        |
| Modal open/close                          | 1.0-2.0s    |
| Dropdown popup render (React Portal)      | 0.7s        |

React SPAs (Khan Academy, Coursera) need LONGER delays than simple pages.
When in doubt, use 2.0s. Too long is slow but works. Too short breaks."""


SECTION_8_RESPONSE = """\
=== YOUR RESPONSE ===

OUTPUT FORMAT: Pure JSON object. No prose preamble. No analysis text.
No "Looking at the screenshot..." narration. No code fences. No markdown.
The FIRST character of your response MUST be `{{` and the LAST character
MUST be `}}`. Reason internally; emit JSON. If you need to express
uncertainty or note an open question, put it inside the JSON as a
"_notes" field — never as free text.

POST to: http://127.0.0.1:5003/api/v1/consult/{{consultation_id}}/respond

JSON payload:
{{
  "screen_type": "DESCRIPTIVE_NAME",
  "tree": {{
    "type": "sequence",
    "children": [...]
  }},
  "extract": {{
    "scope": "web_area",
    "text": [{{"role": "AXStaticText", "parent_role": "AXGroup"}}],
    "images": [{{"source": "window"}}]
  }},
  "expected_next": ["SCREEN_A", "SCREEN_B"],
  "course_id": "{{course_id}}"
}}

RULES FOR screen_type:
- Format is MASTER or MASTER_SUBTYPE where MASTER is EXACTLY one of:
  NAVIGATION, VIDEO, ARTICLE, EXERCISE, TRANSITION, UNKNOWN.
- SUBTYPE must reuse the EXACT subtype name from this platform's knowledge
  (the operational notes section of this prompt shows them) when one fits —
  e.g. EXERCISE_MATCHER, EXERCISE_RADIO, EXERCISE_DROPDOWN. Matching the
  knowledge subtype is what routes the platform's proven notes to future
  encounters; an invented name silently orphans them.
- NEVER invent platform prefixes (no KA_*, no custom families). If no known
  subtype fits, use the bare master (e.g. EXERCISE) — not a new label.
- Be consistent — same screen structure = same screen_type name.

TEMPLATE REUSE (read before designing):
- If the operational notes for the subtype you classified include a VERIFIED
  bt_template or canonical action pattern, ADAPT that template — fill in THIS
  question's values/targets — instead of designing a new structure. Proven
  templates exist precisely so later questions of the same subtype don't get
  novel, unproven BTs. Design from scratch ONLY when no note for your
  subtype carries a pattern.

RULES FOR tree:
- Must be a valid behavior tree with type: sequence at root
- ONLY use registered handlers (see Section 5)
- NO fallback nodes (API rejects them — HTTP 400)
- video_poll must be the ONLY child (no other actions in sequence)
- for_each/conditional parameters at TOP LEVEL, not in params

RULES FOR extract (optional but recommended for content screens):
- scope: "web_area" (standard) or "full" (rare)
- text: array of role/parent_role filters for text extraction
- images: bbox or element-based or source:"window" for image extraction
  Three methods: {{"bbox": [x,y,w,h]}}, {{"role": "AXImage"}}, {{"source": "window"}}

RULES FOR expected_next:
- List screen types that should appear after this BT executes
- NEVER include the current screen_type (creates infinite same-screen loop
  where validation always "passes" and the system never detects being stuck)
- For exercises: include both "next question" screen AND "completion" screen
- For navigation: include the screen types you'd land on after clicking
- Empty list is allowed for terminal screens

SIGNATURE STORAGE:
Screen signatures are stored under the configured TAEY_ED_DATA_DIR (see spark/tasks/paths.py).
Deterministic types (VIDEO, ARTICLE) store the BT with the signature for instant reuse.
Dynamic types (EXERCISE, NAVIGATION, TRANSITION) store the signature for recognition
but always rebuild the BT via Gemini since content changes between encounters.
Build it right -- deterministic BTs are permanent, dynamic BTs set the quality bar."""


def build_section_9(consultation_id: str, context: dict,
                    spark_attempts: int) -> str:
    """Build Section 9: Reconsultation Context."""
    failure_reason = context.get("failure_reason", "unknown")
    previous_screen = context.get("previous_screen_type", "unknown")

    return f"""\
=== RECONSULTATION WARNING ===

This is attempt #{spark_attempts + 1}. Previous tree(s) FAILED.

Previous failure reason: {failure_reason}
Previous screen type: {previous_screen}

YOU MUST:
1. Read /tmp/taey-ed-consult/{consultation_id}/bt_debug.log FIRST
2. Understand WHY the previous tree failed (which handler, what error)
3. Build a FUNDAMENTALLY DIFFERENT tree — not just tweaking params

COMMON FAILURE PATTERNS AND FIXES:
- "Element not found": Wrong target text or role. Check tree.json for actual
  element names. DO NOT use match_mode=contains (forbidden per no-guessing
  rule). Switch to click_at: look at the screenshot, identify the target
  visually, read its visible_bbox from the AX tree, click_at the bbox center.
- "Click had no effect": Wrong click strategy. Try mouse_click if focus_space
  failed. Or the element is behind an overlay — check for modals.
- "Same screen after execute": BT ran but didn't advance. The action target
  may be wrong (clicked wrong button). Or need post_delay for SPA.
- "wrong_answer_same_question": LLM chose wrong answer. Consider using
  solve_complex (multimodal) or add context from extraction.

IF spark_attempts >= 2: Perplexity Deep Research has been invoked for this
screen. Check if knowledge.json was updated with new platform knowledge."""


# =========================================================================
# Main Entry Point: compile_prompt
# =========================================================================

def compile_prompt(
    tree: dict,
    platform: str,
    consultation_id: str,
    context: dict,
    spark_attempts: int = 0,
    is_reconsultation: bool = False,
) -> str:
    """
    Build comprehensive self-contained consultation prompt.

    Target: ~35K-45K characters. Everything needed in one prompt.

    Args:
        tree: Accessibility tree dict from Mac capture_tree
        platform: Platform name (e.g., "khan_academy")
        consultation_id: Unique consultation UUID
        context: Dict with escalation_level, course_id, failure_reason, etc.
        spark_attempts: Number of previous attempts (0 = first consultation)
        is_reconsultation: Whether this is a retry after failure

    Returns:
        Complete prompt string ready for consultation agent.
    """
    tags = analyze_tree(tree)
    sections = []

    # Section 1: Identity & Cardinal Rules (always)
    sections.append(SECTION_1_IDENTITY.format(
        consultation_id=consultation_id,
        platform=platform,
        escalation_level=context.get("escalation_level", "spark_claude"),
        spark_attempts=spark_attempts,
    ))

    # Section 2: Files to Read (always, with reconsult variant)
    sections.append(build_section_2(
        consultation_id, is_reconsultation, context
    ))

    # Section 3 deleted (2026-05-19): tree-tag-driven SCREEN_PATTERNS injection
    # removed per Jesse's directive — knowledge.json operational_notes is now
    # the SINGLE source of truth for screen-specific BT patterns. Every
    # PATTERN_HAS_* template that used to live here has been migrated into
    # the matching subtype's bt_template_hint in knowledge.json under the
    # appropriate master:
    #   HAS_RADIO     -> EXERCISE.subtypes.multiple_choice  (aliases: radio)
    #   HAS_CHECKBOX  -> EXERCISE.subtypes.multiple_select  (aliases: checkbox)
    #   HAS_TEXT_INPUT-> EXERCISE.subtypes.numeric_input + expression_input + free_response
    #   HAS_LINKS     -> NAVIGATION (category-level note)
    #   HAS_VIDEO     -> VIDEO (category-level note)
    #   HAS_COMBOBOX  -> EXERCISE.subtypes.dropdown        (aliases: combobox)
    #   TRANSITION    -> TRANSITION (category-level note)
    # Loader does platform / category / matched-subtype injection via
    # get_operational_notes_for_screen below.

    # Section 4: Platform Knowledge
    # Use knowledge-driven assembly when available, else full docs fallback
    from spark.tasks.knowledge_loader import (
        load_knowledge, load_learned,
        get_handlers_for_screen, get_quirks_for_screen,
        get_question_types_for_screen,
        get_operational_notes_for_screen,
        get_prompt_block_for_screen,
    )
    knowledge = load_knowledge(platform)
    raw_screen_type = context.get("screen_type", "UNKNOWN")
    # Master category for functions that need it (handlers, quirks). Pass the
    # raw variant to operational_notes/prompt_block functions — they do their
    # own master resolution + subtype matching internally.
    try:
        from spark.tasks.screen_type_util import get_master_category
        screen_type = get_master_category(raw_screen_type) or raw_screen_type
    except Exception:
        screen_type = raw_screen_type

    if knowledge and knowledge.get("screen_types"):
        # JIT: Structured knowledge context
        from spark.tasks.classify_screen import _build_knowledge_context
        quirks = get_quirks_for_screen(knowledge, screen_type)
        learned = load_learned(platform, screen_type)
        knowledge_ctx = _build_knowledge_context(knowledge, screen_type, quirks, learned)
        if knowledge_ctx:
            sections.append(knowledge_ctx)

        # Canonical screen pattern (former SCREEN_PATTERNS inline templates,
        # now stored verbatim as prompt_block in knowledge.json per subtype/
        # master). Inject FIRST, before operational_notes bullets, so the
        # worker sees the directive section the way it did pre-migration.
        prompt_block = get_prompt_block_for_screen(knowledge, raw_screen_type)
        if prompt_block:
            sections.append(prompt_block)

        # JIT: Operational notes — supplementary diagnostic learnings.
        # Anti-patterns, gotchas, special-case BTs discovered live. Rendered
        # as markdown bullets, layered on top of the canonical pattern.
        op_notes = get_operational_notes_for_screen(knowledge, raw_screen_type)
        if op_notes:
            sections.append(op_notes)

        # JIT: Selective handler/question type docs
        handler_names = get_handlers_for_screen(knowledge, screen_type, tags)
        question_types = get_question_types_for_screen(knowledge, screen_type, tags)
        sections.append(get_handler_docs(handler_names))
        sections.append(get_question_type_docs(question_types))
    else:
        # Fallback: no knowledge.json — send all docs
        sections.append(
            f"=== PLATFORM KNOWLEDGE ===\n\n"
            f"No knowledge.json exists for {platform}. Use the screenshot and "
            f"tree to determine screen type. After resolving this consultation, "
            f"a knowledge.json should be created via Perplexity Deep Research "
            f"before mapping additional screens."
        )

        sections.append(SECTION_5_HANDLERS_ORIGINAL)
        sections.append(SECTION_6_QUESTION_TYPES_ORIGINAL)

    # Screen session: per-screen working memory (Jesse 2026-06-11) — prior
    # attempts, measured facts, standing plan for THIS screen/question.
    # Injected so builds RESUME instead of re-deriving from zero.
    try:
        from spark.tasks.skeleton import extract_skeleton, skeleton_hash as _ss_hash
        from spark.tasks.skeleton import extract_content_fingerprint as _ss_fp
        from spark.tasks.screen_session import render_for_prompt as _ss_render
        _sess_block = _ss_render(
            platform, _ss_hash(extract_skeleton(tree)), _ss_fp(tree),
        )
        if _sess_block:
            sections.append(_sess_block)
    except Exception:
        logger.exception("screen_session prompt injection failed (continuing)")

    # Section 7: Click Strategies & Timing (always)
    sections.append(SECTION_7_STRATEGIES)

    # Section 8: Response Format (always)
    sections.append(SECTION_8_RESPONSE.format(
        consultation_id=consultation_id,
        course_id=context.get("course_id", "unknown"),
    ))

    # Section 9: Reconsultation (only if applicable)
    if spark_attempts > 0 or is_reconsultation:
        sections.append(build_section_9(
            consultation_id, context, spark_attempts
        ))

    prompt = "\n\n".join(sections)

    logger.info(
        f"Compiled prompt: {len(prompt)} chars, "
        f"{len(tags)} tags: {tags}"
    )

    return prompt
