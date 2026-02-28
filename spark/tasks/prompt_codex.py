# Post-V8 fix (2026-02-20): Fixed $var.items → $var in BT examples (find_all returns list directly)
"""
Prompt Codex — Comprehensive self-contained consultation prompt builder.

Replaces V7's build_consultation_prompt.py (147 lines, empty recipe files)
and classify_archetype.py (182 lines, simple heuristic).

V8 philosophy: Include EVERYTHING the consultation agent needs in one prompt.
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
4. NEVER click "Up next" on Khan Academy (mastery-adaptive, skips content).
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

=== WHAT NOT TO DO (Anti-Patterns from V4-V9) ===
- Use fallback nodes for OPTIONAL steps only (e.g., try Mark Complete, skip if absent)
- NEVER retry same failing tree — different approach each attempt, max 3 total
- NEVER use confidence thresholds — signature matching handles routing
- NEVER hardcode lesson/unit names in targets — use $nav.answer from LLM
- NEVER use poll_interval param on video_poll — handler ignores it, sleeps 30s
- NEVER auto-click "Try again" on wrong answers — creates bot detection risk
- NEVER use `duration` param on wait handler — the param is `seconds`
- NEVER put for_each/conditional params under `params:` — top-level keys only
- NEVER use discover_menu on ARIA comboboxes — AXMenu doesn't exist for them"""


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
# Screen Pattern Constants — one per detected signal
# =========================================================================

PATTERN_HAS_RADIO = """\
=== DETECTED: RADIO BUTTONS (multiple-choice exercise) ===

Your tree has AXRadioButton elements. This is a multiple-choice exercise screen.

COMPLETE BT PATTERN:
{
  "type": "sequence",
  "children": [
    {
      "type": "action",
      "action": "extract_question",
      "params": {
        "question": {"role": "AXStaticText", "parent_contains": "LOOK_AT_TREE"},
        "options": {"role": "AXRadioButton"}
      },
      "store": "q"
    },
    {
      "type": "action",
      "action": "send_to_llm",
      "params": {
        "question": "$q.question_text",
        "question_type": "solve_choice",
        "options": "$q.options",
        "context": "$q.reference_texts"
      },
      "store": "llm"
    },
    {
      "type": "action",
      "action": "find_and_click",
      "params": {
        "target": "$llm.answer",
        "role": "AXRadioButton",
        "strategy": "focus_space",
        "match_mode": "contains"
      }
    },
    {
      "type": "action",
      "action": "find_and_click",
      "params": {
        "target": "Check",
        "role": "AXButton",
        "strategy": "mouse_click",
        "post_delay": 2.0,
        "match_mode": "exact"
      }
    },
    {
      "type": "action",
      "action": "store_qa",
      "params": {
        "question": "$q.question_text",
        "answer": "$llm.answer",
        "question_type": "solve_choice"
      }
    }
  ]
}

HOW IT WORKS:
1. extract_question scopes to AXWebArea, uses your params to find question text
   and radio button options. You MUST set parent_contains to the actual container
   name from the tree where the question lives.
   Returns: {question_text: str, options: [str], reference_texts: [str]}
2. send_to_llm calls Spark /api/v1/generate which routes to Gemini 2.5 Pro.
   Maps options to A/B/C letters, asks LLM for letter, maps back to exact text.
   Returns: {success: true, answer: "exact option text from the list"}
3. find_and_click finds the radio button whose name contains $llm.answer.
   Strategy focus_space: focuses element then presses Space (required for
   browser radio buttons — mouse_click doesn't reliably toggle radio state).
4. find_and_click("Check") submits the answer. post_delay 2.0 gives the SPA
   time to process and load the next question.
5. store_qa saves the Q&A pair to Mac-side SQLite for knowledge building.

EXPECTED_NEXT:
- Same screen type (next question in the exercise)
- TRANSITION (score card or "Next" button after all questions answered)
- NAVIGATION (if exercise completes back to content list)
Do NOT include the current screen in expected_next (creates infinite loops).

WRONG ANSWER DETECTION:
If the Mac reports the same skeleton hash after executing this BT, it means
the same question was re-presented — the answer was wrong. This triggers
reconsultation with failure_reason="wrong_answer_same_question".

VARIANT: If the tree also has AXTextArea (radio + text field combo):
Use question_type="solve_choice" with has_text_field=True in params.
LLM returns {answer: "option text", text_response: "reflection text"}.
Add find_and_type for the text field AFTER clicking the radio button.

VARIANT: If question has images/diagrams visible in screenshot:
Use question_type="solve_complex" instead. This sends the screenshot to
Gemini 2.5 Pro for multimodal analysis.

SUBMIT BUTTON NAMES (varies by platform):
- Khan Academy: "Check" (AXButton)
- Coursera: "Submit" (AXButton)
- Acellus: varies — look at the screenshot"""


PATTERN_HAS_CHECKBOX = """\
=== DETECTED: CHECKBOXES (multi-select exercise) ===

Your tree has AXCheckBox elements. This is a multi-select exercise screen.

COMPLETE BT PATTERN:
{
  "type": "sequence",
  "children": [
    {
      "type": "action",
      "action": "extract_question",
      "params": {
        "question": {"role": "AXStaticText", "parent_contains": "LOOK_AT_TREE"},
        "options": {"role": "AXCheckBox"}
      },
      "store": "q"
    },
    {
      "type": "action",
      "action": "send_to_llm",
      "params": {
        "question": "$q.question_text",
        "question_type": "solve_checkbox",
        "options": "$q.options",
        "context": "$q.reference_texts"
      },
      "store": "llm"
    },
    {
      "type": "action",
      "action": "for_each",
      "items": "$llm.selected",
      "variable": "sel",
      "do": {
        "type": "action",
        "action": "find_and_click",
        "params": {
          "target": "$sel",
          "role": "AXCheckBox",
          "strategy": "focus_space",
          "match_mode": "contains"
        }
      }
    },
    {
      "type": "action",
      "action": "find_and_click",
      "params": {
        "target": "Check",
        "role": "AXButton",
        "strategy": "mouse_click",
        "post_delay": 2.0
      }
    },
    {
      "type": "action",
      "action": "store_qa",
      "params": {
        "question": "$q.question_text",
        "answer": "$llm.selected",
        "question_type": "solve_checkbox"
      }
    }
  ]
}

HOW IT WORKS:
1. extract_question uses your params to find question text and checkbox options.
2. send_to_llm with solve_checkbox asks for comma-separated letter selection.
   Returns: {success: true, selected: ["option text 1", "option text 3"]}
3. for_each iterates $llm.selected, clicking each checkbox via focus_space.
   CRITICAL: for_each params (items, variable, do) go at TOP LEVEL, not in params.
4. find_and_click("Check") submits. post_delay 2.0 for SPA processing.
5. store_qa saves Q&A pair.

WARNING: solve_checkbox has a known 30-char truncation bug on fallback parsing.
Use full option text in the tree, not truncated versions."""


PATTERN_HAS_TEXT_INPUT = """\
=== DETECTED: TEXT INPUT (fill-in-the-blank or short answer) ===

Your tree has AXTextArea or AXTextField elements. This is a text input exercise.

COMPLETE BT PATTERN:
{
  "type": "sequence",
  "children": [
    {
      "type": "action",
      "action": "extract_question",
      "params": {
        "question": {"role": "AXStaticText", "parent_contains": "LOOK_AT_TREE", "min_length": 20},
        "text": [{"role": "AXStaticText", "parent_contains": "LOOK_AT_TREE", "min_length": 10}]
      },
      "store": "q"
    },
    {
      "type": "action",
      "action": "send_to_llm",
      "params": {
        "question": "$q.question_text",
        "question_type": "solve",
        "context": "$q.reference_texts"
      },
      "store": "llm"
    },
    {
      "type": "action",
      "action": "find_and_type",
      "params": {
        "target": "",
        "text": "$llm.answer",
        "role": "AXTextArea"
      }
    },
    {
      "type": "action",
      "action": "find_and_click",
      "params": {
        "target": "Check",
        "role": "AXButton",
        "strategy": "mouse_click",
        "post_delay": 2.0
      }
    },
    {
      "type": "action",
      "action": "store_qa",
      "params": {
        "question": "$q.question_text",
        "answer": "$llm.answer",
        "question_type": "solve"
      }
    }
  ]
}

HOW IT WORKS:
1. extract_question uses your params to find question/prompt text in the tree.
2. send_to_llm with solve generates a text answer.
   Returns: {success: true, answer: "text answer"}
3. find_and_type finds the text field (empty target matches first field)
   and types the answer. If multiple fields, specify target by field label.
4. Submit and store as usual.

NOTE: If both radio buttons AND text fields are present, this is a
choice-with-text combo. See HAS_RADIO variant for has_text_field=True."""


PATTERN_HAS_MANY_LINKS = """\
=== DETECTED: MANY LINKS (navigation / content list) ===

Your tree has 15+ AXLink elements. This is a navigation or content list screen.

COMPLETE BT PATTERN:
{
  "type": "sequence",
  "children": [
    {
      "type": "action",
      "action": "find_all",
      "params": {
        "role": "AXLink"
      },
      "store": "links"
    },
    {
      "type": "action",
      "action": "send_to_llm",
      "params": {
        "question_type": "navigate",
        "items": "$links"
      },
      "store": "nav"
    },
    {
      "type": "action",
      "action": "find_and_click",
      "params": {
        "target": "$nav.answer",
        "role": "AXLink",
        "strategy": "mouse_click",
        "match_mode": "contains",
        "post_delay": 3.0
      }
    }
  ]
}

HOW IT WORKS:
1. find_all collects ALL AXLink elements with their descriptions.
   Each item has: {element, description, popup_desc, label}
   Labels include completion indicators from preceding text.
2. send_to_llm with navigate analyzes completion indicators and picks
   the first incomplete item. Platform-agnostic — looks for "Completed",
   "Not started", checkmarks, "Try again", etc.
   Returns: {success: true, answer: "description text of first incomplete item"}
3. find_and_click navigates to that item. post_delay 3.0 for SPA loading.

EXPECTED_NEXT:
- VIDEO_UNSTARTED, ARTICLE, EXERCISE, QUIZ_INTRO (content landing pages)
- Sub-navigation (deeper content list)

CARDINAL RULE: NEVER hardcode link text in target. ALWAYS use $nav.answer
from the LLM. Link text changes between units/courses."""


PATTERN_HAS_VIDEO = """\
=== DETECTED: VIDEO PLAYER ===

Video screens have 3 states. Detect which one from the tree:

STATE 1 — UNSTARTED:
Signal: "Play" button visible, no "Pause" button
BT: Single find_and_click to start playback
{
  "type": "sequence",
  "children": [
    {
      "type": "action",
      "action": "find_and_click",
      "params": {
        "target": "Play",
        "role": "AXButton",
        "strategy": "mouse_click"
      }
    }
  ]
}
screen_type: VIDEO_UNSTARTED
expected_next: ["VIDEO_PLAYING"]

STATE 2 — PLAYING:
Signal: "Pause" visible, video progress active
BT: ONLY video_poll. No other actions.
{
  "type": "sequence",
  "children": [
    {
      "type": "action",
      "action": "video_poll"
    }
  ]
}
screen_type: VIDEO_PLAYING
expected_next: ["VIDEO_COMPLETE"]
CRITICAL: video_poll MUST be the only child. It sleeps 30s and returns
continue_loop=true. Pipeline re-matches after each poll cycle.

STATE 3 — COMPLETE:
Signal: Sidebar shows checkmark, "Up next" visible, video ended
BT: Click "Next" button (NOT "Up next")
{
  "type": "sequence",
  "children": [
    {
      "type": "action",
      "action": "find_and_click",
      "params": {
        "target": "Next",
        "role": "AXButton",
        "strategy": "mouse_click",
        "post_delay": 3.0
      }
    }
  ]
}
screen_type: VIDEO_COMPLETE
expected_next: ["NAVIGATION", "EXERCISE_RADIO", "ARTICLE"]

CARDINAL RULES:
- NEVER click "Up next" on Khan Academy (mastery-adaptive, skips content)
- NEVER skip or seek (must watch to 100%)
- Check sidebar completion indicator before marking complete"""


PATTERN_HAS_COMBOBOX = """\
=== DETECTED: COMBOBOX / DROPDOWN (matching or selection exercise) ===

Your tree has AXComboBox or AXPopUpButton elements. These are dropdown
selection exercises (common in Khan Academy matching/sorting).

IMPORTANT: ARIA comboboxes do NOT create AXMenu. discover_menu WILL FAIL.
Use keyboard navigation instead.

COMPLETE BT PATTERN (Khan Academy combobox):
{
  "type": "sequence",
  "children": [
    {
      "type": "action",
      "action": "find_all",
      "params": {
        "role": "AXPopUpButton"
      },
      "store": "popups"
    },
    {
      "type": "action",
      "action": "send_to_llm",
      "params": {
        "question_type": "solve_matching",
        "items": "$popups"
      },
      "store": "matches"
    },
    {
      "type": "action",
      "action": "for_each",
      "items": "$popups",
      "variable": "popup",
      "do": {
        "type": "sequence",
        "children": [
          {
            "type": "action",
            "action": "click",
            "params": {
              "element": "$popup.element",
              "strategy": "mouse_click"
            }
          },
          {
            "type": "action",
            "action": "wait",
            "params": {"seconds": 0.7}
          },
          {
            "type": "action",
            "action": "lookup_match",
            "params": {
              "matches": "$matches.matches",
              "key": "$popup.description"
            },
            "store": "chosen"
          },
          {
            "type": "action",
            "action": "find_and_click",
            "params": {
              "target": "$chosen",
              "role": "AXMenuItem",
              "strategy": "mouse_click",
              "match_mode": "contains"
            }
          }
        ]
      }
    },
    {
      "type": "action",
      "action": "find_and_click",
      "params": {
        "target": "Check",
        "role": "AXButton",
        "strategy": "mouse_click",
        "post_delay": 2.0
      }
    }
  ]
}

KEYBOARD NAVIGATION ALTERNATIVE (for React Portal comboboxes):
Instead of click + find_and_click on menu items:
1. click the popup → opens dropdown
2. press_key: down (repeat N times to reach desired option)
3. press_key: return (select)
This avoids React Portal click-capture issues.

WARNING: Combobox options may render in a React Portal (separate subtree).
The AT-SPI tree may show them at a different nesting level."""


PATTERN_TRANSITION = """\
=== DETECTED: TRANSITION SCREEN ===

This screen has buttons or links but no assessment signals (no radio,
checkbox, text field, or quiz markers). It's a transitional page.

Common examples:
- Score card after completing an exercise ("Next" button)
- "Start quiz" button on quiz intro page
- "Continue" button between lessons
- Article page with "Next" at the bottom

COMPLETE BT PATTERN:
{
  "type": "sequence",
  "children": [
    {
      "type": "action",
      "action": "find_and_click",
      "params": {
        "target": "BUTTON_TEXT_FROM_SCREENSHOT",
        "role": "AXButton",
        "strategy": "mouse_click",
        "post_delay": 3.0
      }
    }
  ]
}

HOW TO DETERMINE target:
1. Look at the SCREENSHOT — what button is the clear next action?
2. Common targets: "Next", "Continue", "Start quiz", "Got it"
3. Check the tree for the exact button text
4. NEVER use "Skip" or "Up next" as targets

expected_next: The screen type you'd land on after clicking.
For "Next" after a score card → NAVIGATION or next EXERCISE.
For "Start quiz" → EXERCISE_RADIO or EXERCISE_CHECKBOX."""


# Map tag names to pattern strings
# V20: HAS_MANY_LINKS -> HAS_LINKS, TRANSITION removed (no longer a tag)
SCREEN_PATTERNS = {
    "HAS_RADIO":      PATTERN_HAS_RADIO,
    "HAS_CHECKBOX":   PATTERN_HAS_CHECKBOX,
    "HAS_TEXT_INPUT":  PATTERN_HAS_TEXT_INPUT,
    "HAS_LINKS":      PATTERN_HAS_MANY_LINKS,
    "HAS_VIDEO":      PATTERN_HAS_VIDEO,
    "HAS_COMBOBOX":   PATTERN_HAS_COMBOBOX,
    # TRANSITION pattern still available but not tag-triggered
    # Gemini can reference PATTERN_TRANSITION via classify_screen prompt
}


SECTION_5_HANDLERS = """\
=== HANDLER REFERENCE (16 registered + 2 composable) ===

REGISTERED HANDLERS — Use as action: value in BT nodes.
Any other action name will SILENTLY FAIL (logs error, returns FAILURE).

find_and_click:
  Purpose: Find element by text/role, then click it
  Params:
    target (str, required): Text to search for in element name/description
    role (str, optional): AX role filter (AXButton, AXLink, AXRadioButton, etc.)
    match_mode (str): "exact" or "contains" (default: exact)
    strategy (str): "mouse_click" (default, browsers), "focus_space" (radio/checkbox),
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
    "find_and_click": """\
find_and_click:
  Purpose: Find element by text/role, then click it
  Params:
    target (str, required): Text to search for in element name/description
    role (str, optional): AX role filter (AXButton, AXLink, AXRadioButton, etc.)
    match_mode (str): "exact" or "contains" (default: exact)
    strategy (str): "mouse_click" (default, browsers), "focus_space" (radio/checkbox),
                    "focus_enter" (buttons), "ax_press" (native Mac apps)
    fallback_roles (list): Alternate roles to try if primary role not found
    post_delay (float): Seconds to wait after click (default: 0)
  Returns: {success: true/false, element: {...}}
  4-tier fallback: exact→contains→alternate roles→no role filter""",

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
  Purpose: Click element from blackboard variable
  Params:
    element (ref): Blackboard reference to element dict (e.g., "$_current")
    target (str): Alternative to element — text to find
    role (str): Role filter
    match_mode (str): "exact" or "contains"
    strategy (str): Click strategy
  Note: If element is dict from find_all, re-finds fresh by description""",

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
| ax_press    | Native Mac apps (JavaFX, Cocoa)   | AXPress accessibility action     |

DEFAULT: mouse_click for all browser platforms (Khan Academy, Coursera, etc.)
EXCEPTION: Radio/checkbox use focus_space (mouse_click doesn't reliably toggle)
EXCEPTION: React Portal elements — mouse_click on portal elements may not fire
  React synthetic events. Use keyboard navigation instead (see HAS_COMBOBOX).

find_and_click has a 4-tier fallback chain:
1. Exact match with specified role
2. Contains match with specified role
3. Exact/contains with fallback_roles
4. No role filter (last resort)

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

POST to: http://localhost:5002/api/v1/consult/{{consultation_id}}/respond

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
- Use DESCRIPTIVE names: EXERCISE_RADIO, VIDEO_PLAYING, UNIT_OVERVIEW, etc.
- Be consistent — same screen structure = same screen_type name
- Include platform prefix if ambiguous: KA_EXERCISE_DROPDOWN

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
Screen signatures are stored in JSON files at /var/spark/taey-ed/signatures/{{platform}}.json.
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
  element names. Use match_mode: contains if exact match is too strict.
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

    # Section 3: Screen Patterns (tree-driven)
    for tag in tags:
        if tag in SCREEN_PATTERNS:
            sections.append(SCREEN_PATTERNS[tag])

    # Section 4: Platform Knowledge
    # Use knowledge-driven assembly when available, else full docs fallback
    from spark.tasks.knowledge_loader import (
        load_knowledge, load_learned,
        get_handlers_for_screen, get_quirks_for_screen,
        get_question_types_for_screen,
    )
    knowledge = load_knowledge(platform)
    screen_type = context.get("screen_type", "UNKNOWN")

    if knowledge and knowledge.get("screen_types"):
        # JIT: Structured knowledge context
        from spark.tasks.classify_screen import _build_knowledge_context
        quirks = get_quirks_for_screen(knowledge, screen_type)
        learned = load_learned(platform, screen_type)
        knowledge_ctx = _build_knowledge_context(knowledge, screen_type, quirks, learned)
        if knowledge_ctx:
            sections.append(knowledge_ctx)

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
