# Taey-Ed V8: Complete Rebuild Specification

## Part 0: Why V7 Is Broken Everywhere

You were right. It's not just server.py. After tracing actual code paths across all files, here are the **real bugs** found in supposedly "proven" code. Every one of these has been silently corrupting behavior:

### Bugs in `match_screen.py`
1. **Platform key missing → silent vector death.** If YAML config doesn't have a `platform:` key, it defaults to `"unknown"`. Weaviate queries filter by `platform="unknown"`, match nothing. Falls silently to YAML. No warning logged.
2. **`_check_vector_available()` lies.** It queries aggregate count across ALL platforms. Returns True even when current platform has zero entries. Result: `_try_vector_match()` runs, opens TWO Weaviate connections, finds nothing, falls to YAML. Every. Single. Request.
3. **YAML match with no `tree:` key → infinite consultation loop.** If a YAML screen matches on markers but has no `tree:` key defined, the server sees `matched=True` but `tree=None`, falls through to consultation. Consultation agent builds a BT, stores it in Weaviate. Next time, vector matches, returns stored BT. But if that BT fails, we're back to YAML match with no tree → consultation → same loop.

### Bugs in `screen_memory.py`
4. **`mark_validated` validates the WRONG screen.** When `screen_type` doesn't match any of the first 10 Weaviate results for a skeleton_hash, it falls back to `objects[0]` — the first arbitrary entry. If `screen_type` is empty string (which happens — `lr.screen` can be empty), it skips type matching entirely and validates whatever comes first.
5. **`mark_invalidated` has the same fallback bug.** Can invalidate the wrong screen entry.
6. **Invalidated entries still get served.** The spinal cord's threshold check (`distance < 0.05`) doesn't check the `validated` field. An invalidated BT with a bad answer will execute again before the consultation even starts. Race condition: rapid polling serves the old broken BT while Spark Claude is still thinking.
7. **`store_screen` never updates the vector.** When updating an existing entry, it calls `collection.data.update()` with properties only — no `vector=` parameter. Fine for now (same skeleton = same embedding), but means there's no upgrade path if the embedding model changes.

### Bugs in `server.py` (/next_action)
8. **Consultation skeleton_hash is stale.** When consultation completes, the directive includes `skeleton_hash` from the ORIGINAL consultation tree (saved in `/tmp/taey-ed-consult/{id}/tree.json`). If the page changed while waiting for consultation (user navigated, page refreshed), `mark_validated` updates the wrong Weaviate entry.
9. **Step 2 and Step 2.5 double-call match_screen.** Both run on the same request, each opening Weaviate connections. Step 2 validates against `after_tree`, Step 2.5 matches against current `tree`. Different trees, different results, potential conflicting decisions.
10. **Unrecognized after-tree silently no-ops.** If Step 2 validation finds `after_tree` matches no known screen (`new_screen=None`), it logs a warning and falls through. No recovery action. The pipeline continues with stale state.

### Bugs in `consultation_respond.py`
11. **No bug per se**, but the guard that prevents overwriting validated entries depends on `mark_invalidated` having already run. If the sequence is: wrong answer → `mark_invalidated` → consultation → respond → `store_screen`, it works. But if `mark_invalidated` fails (bug #5), the new BT can't overwrite the old one.

### Bugs in `bt_core.py` / `bt_handlers.py`
12. **`extract_question` fails silently.** If it raises `RuntimeError`, the handler catches it and returns `{"question_text": "", "options": [], "reference_texts": []}` — that's a SUCCESS, not a FAILURE. The BT continues with empty data. `send_to_llm` gets `question=""` and `options=[]`. Gemini gets a nonsense prompt.
13. **Blackboard `$var` → None chains.** If a `store:` action fails (returns None), the key is never written. Later nodes reference `$that_key.field`, resolve to `None`. Passed as `question=None` to Gemini. Silent garbage.
14. **`_continue_loop` never resets within a tree.** Once `video_poll` sets it, the blackboard flag persists. Any sequence that checks it mid-run halts prematurely. Only reset externally between full tree executions.
15. **`solve_checkbox` truncation.** If Gemini returns full text instead of letters, the substring match truncates options to 30 chars. Short options sharing prefixes → wrong selections.
16. **`parse_assessment_response` returns empty list silently.** JSON parse fails → returns `[]` → BT gets `success: True` with `answers: []`. Zero questions answered, BT reports success.
17. **`handle_extraction` storage failure invisible.** `store_content()` failure is caught and logged, but returns `{"extracted": True}`. Pipeline thinks extraction succeeded. Content lost.
18. **Gemini API key loaded inconsistently.** Text path uses `_ensure_gemini()` with global cache. Vision path (`_solve_complex_with_gemini`, `_solve_matching_with_gemini`) loads from `palios-taey-secrets.json` fresh every call. If file missing, vision returns error dict silently.

### Bugs in `pipeline.py` (Mac)
19. **3-second page change timeout may be too short.** Khan Academy React SPA can take 2-5 seconds on content-heavy pages. If timeout expires with hash unchanged, Mac sends `tree_hash_before == tree_hash_after` → Spark detects "stuck" → consultation. False positive stuck detection.
20. **`after_tree` only sent when `success=True AND NOT continue_loop`.** When BT fails (success=False), no after_tree is sent. Spark's Step 2 validation gate (`if lr.after_tree`) is False, so validation is skipped entirely. Failed BTs get no structured analysis of what screen we're actually on.

**Total: 20 verified bugs across 7 files.** This is why nothing works end-to-end.

---

## Part 1: The 6 Standard Screens

From Khan Academy RESEARCH.md (the most complete platform research we have), educational platforms have 6 core screen patterns that cover ~90% of all pages:

### Screen 1: NAVIGATION (Content List)
**What it looks like:** Page with many links (>15). Course overview, unit overview, lesson list.
**Tree signals:** High count of `AXLink` nodes (>15), heading text with course/unit name.
**What to do:** Find all links → LLM picks first incomplete item → click it.
**BT pattern:**
```json
{
  "type": "sequence",
  "children": [
    {"type": "action", "action": "find_all", "params": {"role": "AXLink"}, "store": "links"},
    {"type": "action", "action": "send_to_llm",
     "params": {"question_type": "navigate", "items": "$links"}, "store": "nav"},
    {"type": "action", "action": "find_and_click",
     "params": {"target": "$nav.answer", "role": "AXLink", "strategy": "mouse_click", "match_mode": "contains", "post_delay": 3.0}}
  ]
}
```
**Expected next:** VIDEO_UNSTARTED, ARTICLE, EXERCISE, QUIZ_INTRO
**Cardinal rule:** NEVER hardcode link text. ALWAYS use `$nav.answer`.

### Screen 2: VIDEO
**3 states detected by tree signals:**

| State | Signal | Action |
|-------|--------|--------|
| UNSTARTED | "Play" button visible, no "Pause" | `find_and_click("Play")` |
| PLAYING | "Pause" visible, video progress | `video_poll` (sleep 30s, `continue_loop=True`) |
| COMPLETE | Sidebar shows checkmark, "Up next" visible | `find_and_click("Next")` — NOT "Up next" |

**BT for UNSTARTED:** `find_and_click("Play", role=AXButton, strategy=mouse_click)`
**BT for PLAYING:** Single node: `video_poll` (no params, sleeps 30s, returns continue_loop)
**BT for COMPLETE:** `find_and_click("Next", role=AXButton)`
**Cardinal rules:**
- NEVER click "Up next" (mastery-adaptive, skips content)
- NEVER skip or seek (must watch to 100%)
- Check sidebar completion indicator before proceeding

### Screen 3: EXERCISE (Radio/Choice)
**Tree signals:** 3+ `AXRadioButton` nodes, question text with `?`
**What to do:** Extract question → LLM answers → click answer → click Check
**BT pattern:**
```json
{
  "type": "sequence",
  "children": [
    {"type": "action", "action": "extract_question", "store": "q"},
    {"type": "action", "action": "send_to_llm",
     "params": {"question": "$q.question_text", "question_type": "solve_choice", "options": "$q.options"},
     "store": "llm"},
    {"type": "action", "action": "find_and_click",
     "params": {"target": "$llm.answer", "role": "AXRadioButton", "strategy": "focus_space"}},
    {"type": "action", "action": "find_and_click",
     "params": {"target": "Check", "role": "AXButton", "strategy": "mouse_click", "post_delay": 2.0}},
    {"type": "action", "action": "store_qa",
     "params": {"question": "$q.question_text", "answer": "$llm.answer", "question_type": "solve_choice"}}
  ]
}
```
**Expected next:** Same screen (next question) OR EXERCISE_COMPLETE (score card) OR NAVIGATION
**Wrong answer detection:** If same EXERCISE screen reappears with same skeleton hash, previous answer was wrong → reconsultation

### Screen 4: EXERCISE (Checkbox/Multi-select)
**Tree signals:** 3+ `AXCheckBox` nodes
**BT pattern:** Same as radio but uses `solve_checkbox` question_type and iterates `$llm.selected` list:
```json
{
  "type": "sequence",
  "children": [
    {"type": "action", "action": "extract_question", "store": "q"},
    {"type": "action", "action": "send_to_llm",
     "params": {"question": "$q.question_text", "question_type": "solve_checkbox", "options": "$q.options"},
     "store": "llm"},
    {"type": "action", "action": "for_each", "items": "$llm.selected", "variable": "sel",
     "do": {"type": "action", "action": "find_and_click",
            "params": {"target": "$sel", "role": "AXCheckBox", "strategy": "focus_space"}}},
    {"type": "action", "action": "find_and_click",
     "params": {"target": "Check", "role": "AXButton", "strategy": "mouse_click", "post_delay": 2.0}},
    {"type": "action", "action": "store_qa",
     "params": {"question": "$q.question_text", "answer": "$llm.selected", "question_type": "solve_checkbox"}}
  ]
}
```

### Screen 5: EXERCISE (Text Input)
**Tree signals:** `AXTextArea` or `AXTextField` + question context
**BT pattern:**
```json
{
  "type": "sequence",
  "children": [
    {"type": "action", "action": "extract_question", "store": "q"},
    {"type": "action", "action": "send_to_llm",
     "params": {"question": "$q.question_text", "question_type": "solve"}, "store": "llm"},
    {"type": "action", "action": "find_and_type",
     "params": {"target": "", "role": "AXTextArea", "text": "$llm.answer"}},
    {"type": "action", "action": "find_and_click",
     "params": {"target": "Check", "role": "AXButton", "strategy": "mouse_click", "post_delay": 2.0}},
    {"type": "action", "action": "store_qa",
     "params": {"question": "$q.question_text", "answer": "$llm.answer", "question_type": "solve"}}
  ]
}
```

### Screen 6: TRANSITION (Click-through)
**Tree signals:** Button/link present, no assessment signals (no radio/checkbox/text), no video signals
**Examples:** "Start quiz", "Resume", "Next", "Continue", modal dialogs, loading screens
**BT pattern:** Single `find_and_click` with the target button/link
**Expected next:** Varies (the target screen after the transition)

### Platform-Specific Screens (from RESEARCH.md appendices)

| Screen | Platform | Why Special | BT Pattern |
|--------|----------|-------------|------------|
| EXERCISE_DROPDOWN | Khan Academy | React Portal comboboxes, mouse fails | Keyboard nav: Down arrow × N + Enter |
| EXERCISE_MATCHING | Khan Academy | Perseus drag-and-drop, can't automate | Show hints → "Show solution and move on" |
| ARTICLE | Khan Academy | Modal overlay, extract text | Extract content → close modal |
| QUIZ/UNIT_TEST | Khan Academy | Multi-question assessment | Iterate questions, each is radio/checkbox |

---

## Part 2: How RESEARCH.md Maps to Dynamic Prompts

### The Problem with V7's Approach

V7 had `build_consultation_prompt.py` that loaded recipe cards from `prompts/recipes/*.md` files. TWO critical problems:

1. **Recipe files were never created.** The code loaded empty strings. The only file that existed was `axioms.md` (48 lines). So the consultation agent got ~60 lines of prompt and was told "Read RESEARCH.md" — a separate file it may or may not actually read. Total effective guidance: minimal.

2. **Every time we go too small, we fail.** Jesse's exact words. 25-40 lines is not enough context for an AI agent to build a correct behavior tree from scratch. The agent needs to understand: what handlers exist, what params they take, what click strategies work, what the platform expects, what anti-patterns to avoid, what question types the LLM supports, what AX roles map to what, how for_each syntax works, how blackboard variables resolve. ALL of this in one self-contained prompt.

### The Solution: Comprehensive Self-Contained Prompt (~40K characters)

The consultation prompt must be **self-contained**. The agent should NOT need to read CLAUDE.md, MASTER_PLAN.md, or any other external file to understand what to do. Everything needed is IN the prompt. 40K characters is ~5% of a 200K context window — a tiny fraction.

**Philosophy change:** V7 tried to be surgical (pull only relevant rules). V8 is comprehensive (include everything the agent could need, organized by relevance). The tree scan still happens — it determines which sections are emphasized and what examples are shown — but nothing is omitted.

### Prompt Architecture: 9 Sections, ~40K characters total

The prompt is assembled by `compile_prompt()` in `prompt_codex.py`. It scans the tree to determine what signals are present (radio buttons, checkboxes, video, links, comboboxes, text fields) and assembles sections accordingly. Sections 1-2 and 7-9 are always included. Sections 3-6 are tree-driven.

#### Section 1: Identity & Cardinal Rules (~3,000 chars, always included)

```
=== YOUR ROLE ===
You are building a behavior tree (BT) for an educational platform screen.
This BT will be stored permanently in Weaviate and executed on every future
encounter of this screen structure. It is NOT a one-off instruction.

CONSULTATION: {consultation_id}
PLATFORM: {platform}
ESCALATION LEVEL: {escalation_level} (attempt {spark_attempts})

=== CARDINAL RULES ===
1. FALLBACK NODES ARE BANNED. API rejects type: fallback. Use type: sequence ONLY.
2. Execution uses NAME and ROLE to find elements, NEVER element_id.
   Element IDs in tree.json are for YOUR visual reference only.
3. NEVER target "Skip" buttons. Exercises must be SOLVED or ESCALATED.
4. NEVER click "Up next" on Khan Academy (mastery-adaptive, skips content).
5. NEVER put a screen in its own expected_next (creates silent infinite loops).
6. video_poll must be the ONLY action in its tree. No other children.
   Pipeline re-match loop handles screen transitions after video completes.
7. ONE attempt at wrong answers. Wrong answer = escalation, not retry.
8. Complete BEFORE navigate: answer → submit → wait → next.
9. Every consultation response is PERMANENT (stored in Weaviate automatically).
   Do NOT give one-off instructions. Build a reusable tree.
10. If you don't know what to do, respond with escalation rather than guessing.
    A wrong tree wastes more time than an honest "I don't know."

=== WHAT NOT TO DO (Anti-Patterns from V4-V9) ===
- NEVER create fallback mechanisms — fix root cause or halt
- NEVER retry same failing tree — different approach each attempt, max 3 total
- NEVER use confidence thresholds — vector distance handles routing
- NEVER hardcode lesson/unit names in targets — use $nav.answer from LLM
- NEVER use poll_interval param on video_poll — handler ignores it, sleeps 30s
- NEVER auto-click "Try again" on wrong answers — creates bot detection risk
- NEVER use `duration` param on wait handler — the param is `seconds`
- NEVER put for_each/conditional params under `params:` — top-level keys only
- NEVER use discover_menu on ARIA comboboxes — AXMenu doesn't exist for them
```

#### Section 2: Files to Read (~1,500 chars, always included)

```
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
   Contains: platform, escalation_level, spark_attempts, context.

{if reconsultation}
4. BT DEBUG LOG: /tmp/taey-ed-consult/{consultation_id}/bt_debug.log
   Shows what the PREVIOUS tree tried and exactly where it failed.
   DO NOT output the same tree. Change targeting strategy, click strategy,
   or screen classification based on what you learn from this log.
   Previous failure reason: {failure_reason}
{endif}
```

#### Section 3: Screen Pattern for Detected Type (~4,000-8,000 chars, tree-driven)

This section is selected based on `analyze_tree()` results. Multiple sections can be included if multiple signals are detected (e.g., radio + text field = choice with text response).

**For each detected signal, include the FULL pattern with COMPLETE JSON example:**

Example for HAS_RADIO (~4,000 chars):
```
=== DETECTED: RADIO BUTTONS (multiple-choice exercise) ===

Your tree has {radio_count} AXRadioButton elements. This is a multiple-choice
exercise screen.

COMPLETE BT PATTERN:
{
  "type": "sequence",
  "children": [
    {
      "type": "action",
      "action": "extract_question",
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
1. extract_question scopes to AXWebArea, finds question text (looks for "?"
   or "___" patterns), finds radio button options by their accessible names.
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
Gemini 2.5 Flash for multimodal analysis.

SUBMIT BUTTON NAMES (varies by platform):
- Khan Academy: "Check" (AXButton)
- Coursera: "Submit" (AXButton)
- Acellus: varies — look at the screenshot
```

Similar full patterns for HAS_CHECKBOX, HAS_TEXT_INPUT, HAS_MANY_LINKS, HAS_VIDEO (3-state model with full example for each state), HAS_COMBOBOX (full keyboard navigation walkthrough), TRANSITION.

**Key difference from V7:** Each pattern section is ~2,000-4,000 chars (not 3-5 lines). Includes the complete JSON BT, explanation of how each step works, expected_next guidance, variants, and platform-specific submit button names.

#### Section 4: Platform Knowledge (~6,000-10,000 chars, tree-driven selection from RESEARCH.md)

**RESEARCH.md is NOT just pointed to — relevant sections are INCLUDED in the prompt.**

The `compile_prompt()` function reads the platform's RESEARCH.md and extracts sections relevant to the detected screen type:

```python
ARCHETYPE_TO_RESEARCH_SECTIONS = {
    "HAS_RADIO":      [3, 5, 6, 7, 8],   # Completion model, Buttons/CTAs, Progress nav, AX tree, Edge cases
    "HAS_CHECKBOX":   [3, 5, 6, 7, 8],
    "HAS_TEXT_INPUT":  [3, 5, 7, 8],
    "HAS_MANY_LINKS": [2, 3, 4, 6, 7],   # Navigation, Completion, Ordering, Progress, AX tree
    "HAS_VIDEO":      [3, 5, 7, 8],       # Completion (video rules), Buttons, AX tree, Edge cases
    "HAS_COMBOBOX":   [7, 8, 10, 11, 12], # AX tree, Edge cases, Dropdown appendices
    "TRANSITION":     [2, 4, 5, 7],       # Navigation, Ordering, Buttons, AX tree
}
```

For a Khan Academy radio button exercise, the prompt includes verbatim:
- Section 3 (Completion Model): How mastery levels work, what happens after answering
- Section 5 (Buttons/CTAs): Which buttons are safe to click, avoid "Skip" and "Start over"
- Section 6 (Progress-Aware Nav): Rules for sequential completion
- Section 7 (AX Tree): Expected AX roles, content type identification
- Section 8 (Edge Cases): "Start over" mechanics, question count varies, etc.

This is ~4,000-8,000 chars of platform-specific knowledge embedded directly in the prompt. The agent doesn't need to open a separate file.

**For platforms without RESEARCH.md:** Section 4 says "No RESEARCH.md exists for this platform. Use the screenshot and tree to determine screen type. After resolving this consultation, a RESEARCH.md should be created via Perplexity Deep Research before mapping additional screens."

#### Section 5: Complete Handler Reference (~8,000 chars, always included)

```
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
  Returns: {success: true, items: [{element, description, popup_desc, label}, ...]}
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
  Purpose: Parse question text + options from tree using ctx.extract_config
  Params: None (automatic from ExecutionContext)
  Returns: {question_text: str, options: [str], reference_texts: [str], question_type: str}
  Scopes to AXWebArea, skips browser chrome

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
    "items": "$all_lessons",       ← TOP LEVEL
    "variable": "lesson",          ← TOP LEVEL
    "do": {                        ← TOP LEVEL
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
    "condition": "$has_text",      ← TOP LEVEL
    "then": {...},                 ← TOP LEVEL
    "else": {...}                  ← TOP LEVEL (optional)
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
  Later $key.field resolves to None. PLAN FOR THIS.
```

#### Section 6: LLM Question Types Reference (~4,000 chars, always included)

```
=== LLM QUESTION TYPES (via send_to_llm → /api/v1/generate) ===

All types route through Gemini 2.5 Pro (primary) or Gemini 2.5 Flash.

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
  How: Sends screenshot to Gemini 2.5 Flash for visual question understanding
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
  will generate a text answer instead of failing. ALWAYS double-check spelling.
```

#### Section 7: Click Strategies & Timing (~2,000 chars, always included)

```
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
When in doubt, use 2.0s. Too long is slow but works. Too short breaks.
```

#### Section 8: Response Format & Rules (~2,500 chars, always included)

```
=== YOUR RESPONSE ===

POST to: http://localhost:5002/api/v1/consult/{consultation_id}/respond

JSON payload:
{
  "screen_type": "DESCRIPTIVE_NAME",
  "tree": {
    "type": "sequence",
    "children": [...]
  },
  "extract": {
    "scope": "web_area",
    "text": [{"role": "AXStaticText", "parent_role": "AXGroup"}],
    "images": [{"source": "window"}]
  },
  "expected_next": ["SCREEN_A", "SCREEN_B"],
  "course_id": "{course_id if known}"
}

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
  Three methods: {"bbox": [x,y,w,h]}, {"role": "AXImage"}, {"source": "window"}

RULES FOR expected_next:
- List screen types that should appear after this BT executes
- NEVER include the current screen_type (creates infinite same-screen loop
  where validation always "passes" and the system never detects being stuck)
- For exercises: include both "next question" screen AND "completion" screen
- For navigation: include the screen types you'd land on after clicking
- Empty list is allowed for terminal screens

WEAVIATE STORAGE:
Your response is automatically embedded in Weaviate ScreenEmbedding.
Future encounters of this screen structure will match via vector similarity
and execute YOUR tree directly — zero LLM cost, ~100ms latency.
This is permanent. Build it right.
```

#### Section 9: Reconsultation Context (variable length, only if spark_attempts > 0)

```
{if reconsultation}
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
screen. Check if RESEARCH.md was updated with new platform knowledge.
{endif}
```

### How `compile_prompt()` Assembles the Prompt

```python
def compile_prompt(tree: dict, platform: str, consultation_id: str,
                   context: dict, spark_attempts: int,
                   is_reconsultation: bool = False) -> str:
    """
    Build comprehensive self-contained consultation prompt.
    Target: ~40K characters. Everything needed in one prompt.
    """
    tags = analyze_tree(tree)
    sections = []

    # Section 1: Identity & Cardinal Rules (always)
    sections.append(SECTION_1_IDENTITY.format(
        consultation_id=consultation_id,
        platform=platform,
        escalation_level=context.get("escalation_level", "spark_claude"),
        spark_attempts=spark_attempts
    ))

    # Section 2: Files to Read (always, with reconsult variant)
    sections.append(build_section_2(consultation_id, is_reconsultation, context))

    # Section 3: Screen Patterns (tree-driven — FULL patterns for detected signals)
    for tag in tags:
        sections.append(SCREEN_PATTERNS[tag])
    # If both HAS_RADIO and HAS_TEXT_INPUT detected, add the combo variant

    # Section 4: Platform Knowledge (relevant RESEARCH.md sections included verbatim)
    research_text = load_research_sections(platform, tags)
    if research_text:
        sections.append(f"=== PLATFORM KNOWLEDGE ({platform}) ===\n\n{research_text}")
    else:
        sections.append(f"=== PLATFORM KNOWLEDGE ===\n\nNo RESEARCH.md for {platform}.")

    # Section 5: Handler Reference (always)
    sections.append(SECTION_5_HANDLERS)

    # Section 6: LLM Question Types (always)
    sections.append(SECTION_6_QUESTION_TYPES)

    # Section 7: Click Strategies & Timing (always)
    sections.append(SECTION_7_STRATEGIES)

    # Section 8: Response Format (always)
    sections.append(SECTION_8_RESPONSE.format(
        consultation_id=consultation_id,
        course_id=context.get("course_id", "unknown")
    ))

    # Section 9: Reconsultation (only if applicable)
    if spark_attempts > 0 or is_reconsultation:
        sections.append(build_section_9(consultation_id, context, spark_attempts))

    prompt = "\n\n".join(sections)

    # Sanity check — prompt should be ~35K-45K chars
    logger.info(f"Compiled prompt: {len(prompt)} chars, {len(tags)} tags: {tags}")

    return prompt


def load_research_sections(platform: str, tags: list[str]) -> str:
    """Load relevant RESEARCH.md sections based on detected tree signals."""
    research_path = Path(f"platforms/{platform}/RESEARCH.md")
    if not research_path.exists():
        return ""

    full_text = research_path.read_text()

    # Parse sections by ## headers
    sections = parse_research_sections(full_text)

    # Determine which sections to include based on tags
    needed = set()
    for tag in tags:
        for section_num in ARCHETYPE_TO_RESEARCH_SECTIONS.get(tag, []):
            needed.add(section_num)

    # Always include sections 7 (AX tree) and 8 (Edge cases)
    needed.add(7)
    needed.add(8)

    # Include appendices if relevant tags present
    if "HAS_COMBOBOX" in tags:
        needed.update([10, 11, 12])  # Dropdown appendices

    return "\n\n".join(
        sections[n] for n in sorted(needed) if n in sections
    )
```

### How RESEARCH.md Feeds the Prompt

RESEARCH.md sections are included **verbatim** in the consultation prompt — not just referenced. This is what makes the prompt ~40K characters instead of 25-40 lines.

**RESEARCH.md structure we request from Perplexity (8 sections + appendices):**
1. Platform Overview — course hierarchy, content types, completion model
2. Navigation Structure — controls, sidebars, buttons, URL patterns
3. Completion/Progress Model — how platform shows done/not-done (AX role hints)
4. Content Ordering Rules — what order to complete things, curriculum vs mastery
5. Buttons and CTAs — which are safe, which skip content (with AX roles)
6. Progress-Aware Navigation — rules to avoid skipping, sidebar scanning
7. Accessibility/Tree Expectations — expected AX roles, ARIA patterns, React SPA behavior
8. Edge Cases and Quirks — the weird stuff that breaks automation
9+ Appendices — platform-specific deep dives (e.g., KA combobox, matching exercises)

**Sections 7 and 8 are ALWAYS included** (they're the most critical for BT building).
Other sections are included based on what screen type was detected.
The prompt builder parses RESEARCH.md by `## N.` headers and selects sections programmatically.

---

## Part 3: Every Flow End-to-End

### Flow A: Screen Matching (How we know what screen we're on)

```
Mac captures tree
  → POST /next_action with tree + last_result
  → Spark: extract_skeleton(tree)
    → skeleton = role + sibling index + vertical third (T/M/B)
    → drops all text content (two quizzes with different questions = same skeleton)
    → skeleton_hash = SHA256(skeleton)[:16]
  → Spark: embed_text(skeleton) via Qwen3 → 4096-dim vector
  → Spark: Weaviate cosine search with platform filter

  distance < 0.05  → KNOWN
    → Return stored BT directly
    → Directive: execute_tree

  distance < 0.191 → ISOMORPHIC
    → Same structure, different content
    → Extract dynamic text (the content that skeleton dropped)
    → Use stored BT but let LLM handle the new content
    → Directive: execute_tree

  distance >= 0.191 → UNCHARTED
    → Never seen this structure before
    → Fall to YAML marker matching (if any markers defined)
    → If YAML matches with a tree: execute that tree
    → If YAML matches without a tree: BUG #3, should not happen
    → If nothing matches: request consultation
    → Directive: need_screenshot (if no screenshot) or consulting
```

**What can go wrong:**
- Bug #1: Platform key missing → vector search uses "unknown" filter → always UNCHARTED
- Bug #2: `_check_vector_available` lies → unnecessary Weaviate connections
- Bug #6: Invalidated entry still returned as KNOWN → broken BT re-executes

**Fix:** Vector search MUST check `validated` field. Platform key MUST be required. Availability check should be platform-specific.

### Flow B: Content Extraction (How educational content gets captured)

```
Screen matched → directive includes extract config (from YAML or Weaviate BT)

Mac pipeline:
  → handle_extraction(tree, screenshot, extract_config)
    → extract_text(tree, config.text)
      → DFS walk of AXWebArea subtree
      → Match by role + parent_role + contains
      → Returns list of text strings
    → For each image spec in config.images:
      → crop_image_region(screenshot, bbox) or find_element_bbox(tree, role)
      → POST /api/v1/extract_image → Gemini 2.5 Flash analyzes image
      → Returns description text
    → Join all texts → POST /api/v1/embed → get embedding vector
    → store_content(platform, course_id, texts, embedding) → SQLite

Mac SQLite stores:
  - courses table (platform, course_name)
  - content table (extracted text, embeddings, images, screen_type)
  - qa_pairs table (questions asked, answers given)
  - checkpoints table (screens_completed for crash recovery)
```

**What can go wrong:**
- Bug #17: Storage failure invisible to caller
- `find_element_bbox` returns None for elements not in viewport → image silently skipped
- Embedding API down → logged, extraction "succeeds" without embeddings

**Fix:** Extraction must return honest success/failure. If storage fails, the pipeline should know.

### Flow C: Question Answering (How exercises get solved)

```
BT starts executing:

Step 1: extract_question
  → Scopes to AXWebArea (excludes browser chrome)
  → Finds question text: DFS for "?" or "___" or imperative patterns
  → Finds options: DFS for AXRadioButton/AXCheckBox/AXButton
  → Finds text fields: AXTextArea/AXTextField
  → Determines type: choice / choice_with_text / fill_blank
  → Stores to blackboard: $q = {question_text, options, reference_texts, question_type}

  ⚠️ Bug #12: If extraction fails, returns empty SUCCESS, not FAILURE
  ⚠️ Bug #13: If $q.question_text is "", send_to_llm gets empty question

Step 2: send_to_llm
  → Builds payload: {question, question_type, options, context}
  → For solve_complex: captures fresh screenshot, sends multimodal
  → POST /api/v1/generate → Spark's call_ollama.py

  On Spark:
  → generate_answer() routes by question_type:
    solve_choice → maps options to A/B/C, asks Gemini for letter, maps back to text
    solve_checkbox → asks for comma-separated letters, maps back to texts
    solve → asks for direct answer text
    solve_matching → asks for "1: answer\n2: answer" format
    solve_complex → multimodal Gemini with screenshot
    navigate → asks which item to click first

  Model cascade: Gemini 2.5 Pro → Gemini 2.5 Flash → Claude CLI haiku
  Returns: {success, answer, question_type, model, ...}

  ⚠️ Bug #18: Vision path loads API key per-call (no cache)
  ⚠️ Bug #15: solve_checkbox truncates to 30 chars on fallback parsing

Step 3: Click the answer
  → For radio: find_and_click($llm.answer, role=AXRadioButton, strategy=focus_space)
  → For checkbox: for_each over $llm.selected, click each AXCheckBox
  → For text: find_and_type($llm.answer, role=AXTextArea)
  → For matching: for_each over items, keyboard nav per combobox

Step 4: Submit
  → find_and_click("Check" or "Submit", role=AXButton, strategy=mouse_click, post_delay=2.0)
  → Wait for page response (post_delay handles this)

Step 5: Store Q&A
  → store_qa(question, answer, question_type) → Mac SQLite
```

### Flow D: Same Screen / Expected Next Detection

```
After BT executes:

Mac captures after_tree, computes after_hash

If after_hash == before_hash:
  → Screen didn't change. Two possibilities:
    a) Action had no effect (button didn't work) → BT failure
    b) Page is loading (SPA takes time) → need to wait

  Mac waits up to 3 seconds polling every 0.3s
  If hash changes during wait → capture final after_tree
  If hash still same after 3s → send tree_hash_before == tree_hash_after to Spark

  ⚠️ Bug #19: 3 seconds may not be enough for slow SPA pages

Spark receives last_result in next /next_action call:

Step 2 (Validation):
  If success=True AND after_tree exists AND NOT continue_loop:
    → match_screen(after_tree) to find what screen we landed on
    → Compare with expected_next list from previous directive
    → If landed_screen in expected_next: VALIDATED → mark_validated in Weaviate
    → If landed_screen NOT in expected_next: NOT VALIDATED
      → Check if same exercise re-presented (wrong answer detection)
      → If wrong answer: mark_invalidated → reconsultation
      → If unknown screen: log warning, fall through

  ⚠️ Bug #10: If after_tree matches nothing, silently no-ops
  ⚠️ Bug #8: skeleton_hash from consultation may be stale

Step 2.5 (Stuck Detection):
  If success=True AND NOT continue_loop AND tree_hash_before == tree_hash_after:
    → Action ran but screen didn't change → STUCK
    → Immediately request consultation (no retries, no grace period)
    → Context includes: stuck_screen, failure_reason="BT executed but screen unchanged"

  No retry counters. No grace periods. First stuck = escalate.
```

### Flow E: Consultation (How unknown screens get solved)

```
Trigger: UNCHARTED screen (no vector match, no YAML match)

1. Spark checks: do we have a screenshot?
   No → return {directive: "need_screenshot"}
   Mac captures screenshot, sends in next /next_action

2. Spark creates consultation:
   → /tmp/taey-ed-consult/{consultation_id}/
     - tree.json (accessibility tree)
     - screenshot.png (visual context)
     - metadata.json (platform, escalation_level, screen_hash, status: pending)
     - bt_debug.log (if reconsultation, previous BT execution trace)

3. ONE AT A TIME enforcement:
   → Scan /tmp/taey-ed-consult/ for ANY pending consultation
   → If one exists, return it instead of creating new
   → Never create a second consultation while one is pending

4. Dynamic prompt assembly:
   → analyze_tree(tree) → tags: ["HAS_RADIO"] or ["HAS_MANY_LINKS"] etc.
   → compile_prompt(tree, platform, ...) → ~40K chars of comprehensive, self-contained rules
   → Includes: handler reference, BT patterns, platform knowledge (from RESEARCH.md),
     anti-patterns, question types, click strategies, response format
   → NOT: "Read CLAUDE.md" (1600 lines). NOT: a pointer to RESEARCH.md.
   → The prompt IS the documentation. Agent needs nothing else.

5. Notify Spark Claude via tmux:
   → tmux load-buffer → paste-buffer → Enter
   → Message tells agent to either:
     a) Handle directly (if MCP tools needed for research)
     b) Launch subagent with the compiled prompt

6. Spark Claude (or subagent):
   → Reads screenshot + tree
   → Sees ONLY the relevant rules (e.g., "RADIO BUTTONS DETECTED, pattern is...")
   → Builds BT → POSTs /api/v1/consult/{id}/respond
   → Response: {screen_type, tree (the BT), expected_next}
   → Auto-embedded in Weaviate as PROVISIONAL (validated=False)

7. Mac polls /next_action → Spark sees consultation complete
   → Returns {directive: "execute_tree", tree: <the BT>}

8. Mac executes → reports result → Spark validates (Flow D)

9. Escalation:
   → spark_attempts >= 2 → Perplexity Deep Research (Tier 2)
   → spark_attempts >= 3 → User dialog (Tier 3)
   → After Tier 3 → STOP
```

---

## Part 4: File-by-File Honest Assessment

### Mac Side (`app/`)

| File | Lines | Verdict | Issues Found |
|------|-------|---------|--------------|
| `pipeline.py` | 563 | **REWRITE partially** | Bug #19 (3s timeout too short), Bug #20 (no after_tree on failure). Page change timeout needs to be configurable. Should send after_tree even on failure for better Spark diagnostics. |
| `bt_core.py` | 359 | **REWRITE partially** | Bug #12 (silent empty return), Bug #13 ($var→None chains), Bug #14 (_continue_loop never resets). The BT engine fundamentally works but error handling is broken — failures masquerade as successes. |
| `bt_handlers.py` | 564 | **REWRITE partially** | Bug #12 (extract_question handler catches RuntimeError, returns empty SUCCESS). Bug: send_to_llm doesn't validate response has "answer" key. solve_assessment_page returns success:True even when individual questions fail. |
| `bt_helpers.py` | 256 | **KEEP** | Tree walking utilities. Pure functions. No integration bugs found. |
| `find_element.py` | 228 | **KEEP** | Element search with retry. Works correctly. Web-area scoping prevents chrome pollution. React Portal fallback exists. |
| `click_element.py` | 263 | **KEEP** | 4 click strategies. Off-screen handling. Bounds validation. Known Chrome AXPress issue documented (use mouse_click). |
| `capture_tree.py` | 124 | **KEEP** | Pure accessibility tree capture. No bugs found. |
| `capture_macapptree.py` | 122 | **KEEP** | Tree + screenshot. Finds largest window. Works. |
| `compute_tree_hash.py` | 59 | **KEEP** | SHA256 of sorted role:name pairs. Deterministic. Must match Spark side. |
| `extract_question.py` | 281 | **REWRITE partially** | Question extraction itself is ok. But: first-match DFS for question text may return wrong node when multiple matches. Should return most prominent (largest container) not deepest-first. Reference texts uses different matching logic than extract_text.py — inconsistent. |
| `extract_text.py` | 109 | **KEEP** | YAML-driven text extraction. Works. |
| `handle_extraction.py` | 154 | **REWRITE partially** | Bug #17 (storage failure invisible). Must propagate failure honestly. |
| `call_spark.py` | 60 | **KEEP** | Clean HTTP client. No bugs. |
| `type_text.py` | 48 | **KEEP** | Direct AX API text input. Works. |
| `wait.py` | 56 | **KEEP** | Element polling. 60s max timeout. Works. |
| `build_kb_context.py` | 62 | **KEEP** | SQLite context search. Simple keyword matching. Works. |
| `store_qa.py` | 28 | **KEEP** | Thin wrapper. Works. |
| `crop_image.py` | 109 | **KEEP** | PIL image crop. Works. |
| `checkpoint.py` | 88 | **KEEP** | SQLite crash recovery. Works. |
| `config.py` | 87 | **KEEP** | Config loading. Works. |
| `window.py` | 956 | **KEEP** | Tkinter GUI. Works (functional, not pretty). |
| `browser_url.py` | 148 | **KEEP** | URL verification. Works. |
| `behavior_tree.py` | 26 | **KEEP** | Re-export wrapper. |

**Mac summary:** 11 files KEEP as-is. 5 files need partial rewrite (pipeline, bt_core, bt_handlers, extract_question, handle_extraction). All rewrites are targeted fixes to specific bugs, not full file rewrites.

### Spark Side (`spark/`)

| File | Lines | Verdict | Issues Found |
|------|-------|---------|--------------|
| `server.py` | 1062 | **REWRITE** | Bugs #8, #9, #10. Too many code paths. Double match_screen calls. Stale skeleton_hash. Unrecognized after-tree silent no-op. Must be split into small files. |
| `match_screen.py` | 292 | **REWRITE** | Bugs #1, #2, #3. Platform key silent failure. Aggregate availability check. YAML-without-tree trap. |
| `screen_memory.py` | 435 | **REWRITE partially** | Bugs #4, #5, #6, #7. mark_validated/invalidated fallback-to-first bug. Invalidated entries still served. No vector update path. Fix: add validated filter to queries, remove objects[0] fallback, require screen_type. |
| `consultation_request.py` | 359 | **REWRITE** | Replace prompt building with codex. Keep 1-at-a-time enforcement and escalation logic. Keep RESEARCH.md gate. Remove dead code paths from pre-Holy-Grail era. |
| `skeleton.py` | 205 | **KEEP** | Extraction and hashing work correctly. Deterministic. No bugs found. |
| `screen_router.py` | 163 | **KEEP with fix** | Works but needs to pass `validated=True` filter to Weaviate query (Fix for Bug #6). |
| `screen_collapse.py` | 111 | **KEEP** | Stores successful BT. Works. |
| `call_ollama.py` | 1127 | **REWRITE partially** | Bug #15 (checkbox 30-char truncation), Bug #16 (assessment returns empty success), Bug #18 (vision API key per-call). Core answer generation works. Fix specific bugs. |
| `call_vision.py` | 222 | **KEEP** | Gemini Flash image analysis. Works. |
| `call_embedding.py` | 129 | **KEEP** | Embedding client. Works. |
| `classify_archetype.py` | 182 | **REPLACE** | Replaced by `prompt_codex.py` `analyze_tree()`. Same role-counting concept, integrated with prompt building. |
| `build_consultation_prompt.py` | 147 | **REPLACE** | Replaced by `prompt_codex.py` `compile_prompt()`. V7 loaded empty recipe files → gave agent ~60 lines. V8 gives ~40K chars self-contained. |
| `consultation_state.py` | 79 | **KEEP** | Escalation state tracking. Works. |
| `consultation_respond.py` | 163 | **KEEP with awareness** | Bug #11 (depends on mark_invalidated working). Works if mark_invalidated is fixed. |
| `consultation_escalate.py` | 67 | **KEEP** | Tier advancement. Works. |
| `atomic_write.py` | 32 | **KEEP** | Atomic JSON writes. Works. |
| `notify_tmux.py` | 90 | **KEEP** | Tmux injection. Proven pattern. |
| `validate_config.py` | 250 | **KEEP** | YAML validation. Works. |
| `load_yaml.py` | 55 | **KEEP** | Mtime-cached YAML loader. Works. |
| `screen_router.py` | 163 | **KEEP with fix** | (listed above) |
| `action_review.py` | 284 | **KEEP** | Post-action review. Works. |
| `validate_action.py` | 187 | **KEEP** | Action validation. Works. |

**Spark summary:** 11 files KEEP as-is. 3 files KEEP with targeted fix. 4 files need substantial rewrite (server.py → split, match_screen.py, consultation_request.py, screen_memory.py). 2 files replaced by codex (classify_archetype.py, build_consultation_prompt.py).

### Prompt Assets

| File | Verdict | Reason |
|------|---------|--------|
| `prompts/axioms.md` | **DELETE** | Absorbed into prompt_codex.py Section 5 (handler reference) and Section 1 (cardinal rules). The 48-line axioms file was the ONLY prompt asset that actually existed in V7. |
| `prompts/recipes/*.md` | **DELETE** | These files NEVER EXISTED on disk. build_consultation_prompt.py loaded empty strings. V8's prompt_codex.py has the patterns as Python string constants. |
| `prompts/warnings/*.md` | **DELETE** | Also never existed. V8 includes platform knowledge by extracting RESEARCH.md sections directly. |
| `platforms/*/config.yaml` | **KEEP** | Safety halt patterns, platform metadata |
| `platforms/*/RESEARCH.md` | **KEEP (CRITICAL)** | Platform research. V8's `load_research_sections()` parses these by section headers and includes relevant sections VERBATIM in the ~40K char consultation prompt. This is the primary source of platform-specific knowledge. |

---

## Part 5: Implementation Order

### Phase 1: Fix Mac-Side Bugs (5 targeted changes)

1. **`bt_core.py`**: `extract_question` handler must return FAILURE (not empty SUCCESS) when extraction fails. Add: `if not result.get("question_text"): return {"success": False, "error": "no question found"}`

2. **`bt_core.py`**: Reset `_continue_loop` at the START of each `tick_node` for sequence nodes, not just between full tree executions.

3. **`bt_handlers.py`**: `send_to_llm` must validate response has `answer` or `selected` key. If missing, return `{"success": False, "error": "LLM returned no answer"}`.

4. **`pipeline.py`**: Make page change timeout configurable (`PAGE_CHANGE_TIMEOUT = 5.0` not 3.0). Send after_tree even on BT failure (stripped, for Spark diagnostics).

5. **`handle_extraction.py`**: Propagate storage failure. Return `{"extracted": False, "error": str(e)}` when `store_content` fails.

### Phase 2: Fix Spark-Side Bugs (targeted changes to existing files)

6. **`screen_memory.py`**: Remove `objects[0]` fallback in `mark_validated` and `mark_invalidated`. If screen_type not found, log ERROR and return without modifying anything. Require non-empty screen_type.

7. **`screen_memory.py`**: Add `validated` filter to Weaviate queries in `query_nearest`. Only return validated entries for KNOWN threshold. ISOMORPHIC can include provisional.

8. **`call_ollama.py`**: Fix checkbox 30-char truncation — use full option text for substring matching. Fix assessment empty-list return — return `success: False` when JSON parse fails. Consolidate API key loading to single cached function.

9. **`match_screen.py`**: Require `platform` key in config (error if missing). Change `_check_vector_available` to query with platform filter. Remove YAML-without-tree possibility (validate on load).

### Phase 3: Rewrite the Glue (new files)

10. **Create `spark/tasks/prompt_codex.py`** (~800-1000 lines): The comprehensive prompt builder.

    This is the heart of V8. Contains:
    - `analyze_tree(tree)` — Scans AXWebArea for role counts and text signals (~50 lines, adapted from V7's `classify_archetype.py`)
    - `compile_prompt(tree, platform, consultation_id, context, spark_attempts)` — Assembles the ~40K character prompt from 9 sections (~100 lines of assembly logic)
    - `SECTION_1_IDENTITY` — Cardinal rules, anti-patterns (~3K chars as string constant)
    - `SCREEN_PATTERNS` dict — Full BT JSON + explanation for each detected signal: HAS_RADIO, HAS_CHECKBOX, HAS_TEXT_INPUT, HAS_MANY_LINKS, HAS_VIDEO (3 states), HAS_COMBOBOX, TRANSITION (~4K chars each, ~28K total)
    - `SECTION_5_HANDLERS` — Complete 16-handler reference with all params (~8K chars)
    - `SECTION_6_QUESTION_TYPES` — All 7 question types with I/O formats (~4K chars)
    - `SECTION_7_STRATEGIES` — Click strategies and timing tables (~2K chars)
    - `SECTION_8_RESPONSE` — JSON format, expected_next rules (~2.5K chars)
    - `load_research_sections(platform, tags)` — Parses RESEARCH.md by `## N.` headers, includes relevant sections verbatim based on detected tree signals (~50 lines)
    - `build_section_2/9` — File paths and reconsultation context (~30 lines)

    **Why ~1000 lines:** The prompt text itself is ~40K characters stored as Python string constants. At ~40 chars/line average, that's ~1000 lines. The assembly logic is only ~200 lines. The rest is the actual prompt content that the consultation agent receives.

    **Testing approach:** `python -c "from spark.tasks.prompt_codex import compile_prompt; print(len(compile_prompt(fake_tree, 'khan_academy', 'test', {}, 0)))"` → should print ~38000-42000.

11. **Split `server.py` into:**
    - `spark/server.py` (~100 lines): FastAPI app, middleware, route imports
    - `spark/routes/health.py` (~30 lines): GET /health, GET /screen-memory/stats
    - `spark/routes/next_action.py` (~200 lines): POST /next_action — THE state machine
    - `spark/routes/consultation.py` (~80 lines): Consultation CRUD
    - `spark/routes/compute.py` (~60 lines): /extract_image, /embed, /generate
    - `spark/routes/review.py` (~50 lines): Action review endpoints

12. **Rewrite `consultation_request.py`** (~200 lines): Wire prompt_codex.compile_prompt() instead of build_consultation_prompt. Keep 1-at-a-time enforcement. Keep escalation. Keep RESEARCH.md gate. Remove dead code.

### Phase 4: Test Each Flow

13. **Test screen matching:** Send known tree → verify KNOWN match → verify correct BT returned
14. **Test consultation:** Send unknown tree → verify consultation created → verify prompt is ~40K chars (self-contained, includes handler ref + platform knowledge) → verify BT stored in Weaviate after response
15. **Test stuck detection:** Send same tree twice with same hash → verify immediate consultation
16. **Test question answering:** Mock exercise tree → verify extract → verify LLM call → verify answer click
17. **Test escalation:** Fail 3 times → verify Tier 2 (Perplexity) → fail again → verify Tier 3 (user)

### Phase 5: Deploy and Run on Khan Academy

18. Start API: `uvicorn spark.server:app --host 0.0.0.0 --port 5002`
19. Build Mac app: `python setup.py py2app`
20. Run on KA course page
21. Monitor: first screen → consultation → BT → execute → validate
22. Target: 10+ screens without manual intervention

---

## Part 6: What Success Looks Like

### Minimum Viable Demo (10 days)
- Navigate from KA course page to first video
- Play video to completion (video_poll)
- Navigate to first exercise
- Answer 3+ multiple choice questions correctly
- Move to next content item
- All without manual intervention

### What We're NOT Building
- No seeding. Organic learning only. First encounter = consultation.
- No new platforms. KA only until it works.
- No new BT handlers. 16 is enough.
- No new models. Gemini + Qwen3 + Claude CLI.
- No clever abstractions. Minimal code to fix the 20 bugs and wire the codex.

---

## Part 7: Services Required (Unchanged)

| Service | Endpoint | Purpose |
|---------|----------|---------|
| Weaviate | 192.168.100.10:8088 | Screen vector memory |
| Embedding LB | 192.168.100.10:8091 | Qwen3 skeleton embedding |
| Gemini API | Google Cloud | Answer generation + image analysis |
| Redis | 192.168.100.10:6379 | Session state |
| Neo4j | 192.168.100.10:7689 | Task persistence |

---

## Part 8: File Count

**Total files to touch: 16**
- 5 Mac-side targeted bug fixes (not full rewrites)
- 4 Spark-side targeted bug fixes
- 1 new file (prompt_codex.py, ~800-1000 lines — the consultation prompt is ~40K chars stored as string constants + ~200 lines of assembly logic)
- 6 files from server.py split (server.py + 5 route files)

**Total files untouched: ~30**
- All Mac files that passed audit (find_element, click_element, capture_tree, etc.)
- All Spark files that passed audit (skeleton, screen_collapse, call_vision, etc.)
- Platform configs, RESEARCH.md

**Files deleted: 5**
- `prompts/axioms.md` (absorbed into prompt_codex.py)
- `prompts/recipes/*.md` (never existed, concept replaced by prompt_codex.py)
- `prompts/warnings/*.md` (never existed, replaced by RESEARCH.md sections)
- `classify_archetype.py` (replaced by prompt_codex.py analyze_tree())
- `build_consultation_prompt.py` (replaced by prompt_codex.py compile_prompt())

**No seeding. No shortcuts. Fix the 20 bugs. Build the comprehensive prompt codex. Ship.**

---

## Part 9: Why 40K Characters (Jesse's Mandate)

### The Pattern of Failure

Every time we tried to minimize the consultation prompt, it failed:

1. **V7 CLAUDE.md dump (1600 lines, ~70K chars):** Too noisy. Agent couldn't find the relevant rules. Most of the content was about identity, philosophy, infrastructure — not BT building.

2. **V7 build_consultation_prompt.py (~60 lines, ~3K chars):** Too sparse. Recipe files never existed on disk. Agent got: role anchor + file paths + empty recipe + axioms.md (48 lines). Not enough context to build correct BTs.

3. **V8 first draft codex (25-40 lines, ~2K chars):** Jesse rejected immediately. "No chance it is 25-40 lines. 40K characters is fine. That is tiny amount of context window. Every time you try to go too small you fail."

### The Math

- Claude Code context window: 200K tokens (~800K characters)
- 40K character prompt: **5% of context window**
- A consultation handles ONE screen. 40K chars gives the agent EVERYTHING it needs for that one screen.
- Cost: negligible. Time: negligible. Risk of failure from missing context: eliminated.

### What 40K Characters Buys You

The agent reading the consultation prompt will know:
- Every handler name, every param, every return type
- The exact BT pattern for the detected screen type with full JSON examples
- Platform-specific quirks (from RESEARCH.md, included verbatim)
- Which buttons are safe to click and which skip content
- How AX roles map to HTML elements on this platform
- Every anti-pattern to avoid (from 6 weeks of V4-V9 failures)
- Every question type the LLM supports with input/output formats
- Click strategies, timing guidelines, post_delay values
- Response format, expected_next rules, Weaviate storage behavior
- Previous failure context (if reconsultation)

The agent does NOT need to:
- Read CLAUDE.md
- Read MASTER_PLAN.md
- Read RESEARCH.md separately (relevant sections already included)
- Read any file other than screenshot.png, tree.json, and metadata.json
- Guess about handler params or BT syntax
- Wonder whether to use mouse_click or ax_press

---

## Part 10: 2-Step Screen Classification (Feb 21, 2026)

**Supersedes**: Step 5 (navigation auto-detect) in `next_action.py` and the
heuristic `analyze_tree()` role-counting approach in `prompt_codex.py`.

### Why the Old Approach Failed

1. **Step 5** counted links (>=5) and auto-detected "navigation" screens. But
   nearly every Coursera page has a sidebar with links to all modules, so Step 5
   fired on almost every screen — quizzes, videos, articles, everything.

2. **`analyze_tree()`** counted AX roles (radio buttons, checkboxes, etc.) to
   guess screen type via heuristic. Same approach that caused the auto-classify
   infinite loop disaster. Counting roles is not classification.

3. **`prompt_codex.compile_prompt()`** sent a monolithic ~40K char prompt with
   ALL patterns to a consultation agent via tmux. Most of that content was
   irrelevant to the specific screen being classified.

### The New Process

When a screen has no Weaviate match, it goes through 2 steps. This only happens
the FIRST TIME a screen structure is encountered. After that, Weaviate serves
the stored BT directly.

#### Step A: Classification (Gemini API call)

Send to Gemini directly (not through tmux/Spark Claude):
- Full accessibility tree (always — we don't know what's important)
- Screenshot (base64)
- Platform name
- List of universal screen categories (see below)
- Platform-specific screen variants from RESEARCH.md (if available)

Gemini returns:
- `screen_type`: One of the universal categories
- `confidence_note`: Brief explanation of why (for logging/debugging)
- `platform_variant`: Platform-specific subtype if relevant (e.g., "EXERCISE_DROPDOWN")

Prompt is small and focused. No BT patterns, no handler references, no 40K dump.
Just: "Look at this screen. What type is it? Here are the categories."

#### Step B: Action (depends on classification)

**For deterministic screens** (VIDEO, ARTICLE, TRANSITION):
- Build BT from a predefined template for that type
- Store in Weaviate immediately
- Return `execute_tree`
- No second LLM call needed
- Future encounters match in Weaviate, zero LLM cost

**For interactive screens** (NAVIGATION, EXERCISE, ASSESSMENT):
- Send Gemini NARROWED instructions specific to that screen type only
- Not the full prompt_codex — just the relevant pattern + handler subset
- Gemini returns the BT
- Store in Weaviate as provisional
- Future encounters match in Weaviate

### Universal Screen Categories

These are global categories validated against IMS Caliper Analytics profiles,
Moodle activity taxonomy, edX XBlock types, and Canvas assessment types.
They apply to ALL educational platforms. They are NOT platform-specific.

**Source**: Perplexity Deep Research, Feb 21 2026 — cross-referenced against
IMS Caliper (NavigationEvent, MediaEvent, ReadingEvent, AssessmentItemEvent,
ForumEvent), edX XBlocks (html, video, problem, discussion), Moodle activities
(Page, Video, Quiz, Forum), and Canvas (Quiz, Discussion Board).

| # | Category | Caliper Equivalent | Description | Action Pattern |
|---|----------|--------------------|-------------|----------------|
| 1 | **NAVIGATION** | NavigationEvent | Dashboards, menus, module lists, unit overviews. Primary action: pick which content to go to next. | LLM picks first incomplete item, click |
| 2 | **VIDEO** | MediaEvent | Video lessons, embedded video. Sub-states: unstarted (Play), playing (poll), complete (Next). | Template per sub-state |
| 3 | **ARTICLE** | ReadingEvent | Reading pages, HTML content, static lessons. Scroll, mark complete, advance. | Extract content, click Next |
| 4 | **EXERCISE** | AssessmentItemEvent | Any quiz/assessment screen: MCQ (radio), multi-select (checkbox), fill-blank (text), dropdown, matching. Single or multi-question — the interaction pattern is the same: extract → generate answer → enter → submit. | Template structure, LLM for answers |
| 5 | **TRANSITION** | (no standard equiv) | Loading screens, score cards, "Start quiz", "Continue", "Resume", completion messages, confirmation modals. Single action to advance. | Click target button |
| 6 | **UNKNOWN** | N/A | Anything that doesn't fit the above. Do NOT guess — escalate. | Escalate to consultation/user |

**ASSESSMENT merged into EXERCISE**: Every platform (Canvas, Moodle, edX) treats
single-question and multi-question assessments identically. A 20-question exam is
just EXERCISE repeated N times with a submit at the end — a sequencing concern,
not a category concern. If needed later, add `multi: true` sub-flag.

**DISCUSSION deferred**: Forum posts are a real distinct type (Caliper ForumEvent,
edX discussion XBlock) but the automation pattern is complex (generate student-like
posts, read/reply to peers) and we haven't encountered one. UNKNOWN catches these
and triggers consultation when they appear.

Platform-specific variants (from RESEARCH.md) refine these categories:
- Khan Academy: EXERCISE_DROPDOWN (React combobox), EXERCISE_MATCHING (drag-drop)
- Coursera: ARTICLE variant with "Mark as completed" button
- Acellus: strictly linear, no skipping

The classification prompt includes these 6 categories plus any platform-specific
variants from the platform's RESEARCH.md.

### What Changes in Code

| File | Change | Reason |
|------|--------|--------|
| `next_action.py` | Remove Step 5 (lines 388-444) | Hardcoded nav BT based on link count |
| `next_action.py` | Step 6 calls classify → then acts | 2-step process |
| NEW: `classify_screen.py` | Gemini API call for classification | Step A |
| NEW: `build_screen_bt.py` | Template BTs + narrowed LLM instructions | Step B |
| `prompt_codex.py` | No longer used for classification | Replaced by 2-step |
| `consultation_request.py` | May be simplified | No more tmux chain for first classification |

### What Does NOT Change

- Mac capture (capture_tree, capture_macapptree, compute_tree_hash) — FROZEN
- Weaviate storage/retrieval (screen_memory, match_screen) — FROZEN
- BT execution engine (bt_core, bt_handlers) — FROZEN
- Escalation chain (spark_claude → perplexity → user) — still exists for failures
- RESEARCH.md files — still platform knowledge source

### Change Log

| Date | Change | Status |
|------|--------|--------|
| Feb 21 | Remove Step 5 (nav auto-detect) | Approved, not implemented |
| Feb 21 | 2-step classification process defined | Approved, not implemented |
| Feb 21 | Universal screen categories (6 active + UNKNOWN) | Validated via Perplexity/Caliper |
| Feb 21 | ASSESSMENT merged into EXERCISE | Validated via Perplexity |
| Feb 21 | DISCUSSION deferred (UNKNOWN catches it) | Validated via Perplexity |
