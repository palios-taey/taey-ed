"""
Answer generation for educational quiz questions.

Primary: Gemini 2.5 Pro (paid tier) for ALL question types.
Fallback: Claude CLI (sonnet) if Gemini fails.

Renamed from call_ollama.py (2026-02-27) -- no Ollama models are used.

Supports question types:
- solve_choice: Pick the correct option from multiple choices
- solve: Generate a text answer (fill-in-the-blank, short answer)
- solve_checkbox: Select all correct answers from options
- solve_complex: Complex screen → Gemini vision (screenshot + text)
- solve_matching: Match items to options → Gemini vision if screenshot
- solve_assessment: Full multi-question graded assessment
- navigate: Pick first incomplete item from a list

Cost: ~$0.003/call at Gemini 2.5 Pro pricing ($1.25/MTok in, $10/MTok out).
Spark provides COMPUTE only - Mac handles execution.
"""

import asyncio
import httpx
import json
import logging
from typing import List, Optional, Dict

logger = logging.getLogger(__name__)

# =============================================================================
# CONFIGURATION
# =============================================================================
GEMINI_MODELS = ["gemini-2.5-pro"]  # DEPRECATED 2026-05-12 (Jesse: no Gemini path). Kept for back-compat refs.
CLAUDE_CLI_MODEL = "claude-opus-4-7"  # Primary (and only) LLM per Jesse 2026-05-12.


# =============================================================================
# PROMPTS
# =============================================================================

SOLVE_CHOICE_PROMPT = """You are answering an educational quiz question. Pick the correct answer from the lettered options below.

{context_block}
Question: {question}

Options:
{options_block}

IMPORTANT: Reply with ONLY the letter (A, B, C, etc.) of the correct answer. Nothing else. Just the letter."""


SOLVE_CHOICE_WITH_TEXT_PROMPT = """You are answering an educational quiz question that requires BOTH selecting an option AND writing a brief reflection.

{context_block}
Question: {question}

Options:
{options_block}

Reply in exactly this format (two lines only):
ANSWER: [letter]
REFLECTION: [1-2 sentence reflection explaining your choice]"""


SOLVE_CHECKBOX_PROMPT = """You are answering an educational quiz question. Select ALL correct answers from the lettered options below.

{context_block}
Question: {question}

Options:
{options_block}

IMPORTANT: Reply with ONLY the letters of ALL correct answers, separated by commas. Example: A, C, D
If only one answer is correct, reply with just that letter. No explanation. Just the letters."""


SOLVE_TEXT_PROMPT = """You are answering an educational question. Give a precise, correct answer.

{context_block}
Question: {question}

Reply with ONLY the answer. No explanation, no preamble. Just the answer value.
For math: give the number only (e.g., "42" not "The answer is 42").
For text: give the shortest correct response."""


SOLVE_ASSESSMENT_PROMPT = """You are answering a graded assessment for an online educational course. Answer ALL questions below correctly.

{context_block}

{questions_block}

Return your answers as valid JSON only. For RADIO questions, return the EXACT text of the ONE correct option. For CHECKBOX questions, return a list of the EXACT texts of ALL correct options.

Format:
{{
  "answers": [
    {{"question_index": 0, "type": "radio", "selected": "exact option text here"}},
    {{"question_index": 1, "type": "checkbox", "selected": ["exact text 1", "exact text 2"]}}
  ]
}}

IMPORTANT: Return ONLY valid JSON. No explanation. Use EXACT option text as shown above - copy it character for character."""


NAVIGATE_PROMPT = """You are helping navigate an educational platform. Look at the screenshot and the list of clickable items below. Select the FIRST incomplete content item in strict curriculum order.

RULES:
1. IGNORE these — they are NOT curriculum items:
   - Site navigation (logos, search, "skip to content", login, etc.)
   - Recommendation sections ("Up next for you", "Recommended", "Continue where you left off", etc.)
   - Footer links (About, Donate, Privacy, social media, etc.)
2. Use the screenshot to determine completion status. Completed items have checkmarks, green indicators, or "Completed" text. Incomplete items have no indicator or say "Not started", "unfamiliar", etc.
3. Follow STRICT curriculum order: go through items top to bottom as numbered. Pick the FIRST incomplete curriculum item. Do NOT jump ahead based on visual prominence or recommendations.
4. Within the SAME section, videos and articles must be completed before exercises.

Clickable items (in page order):
{items_block}

Reply with ONLY the exact DESCRIPTION text of the first incomplete curriculum item. Nothing else. Just the description text, copied exactly."""


SOLVE_MATCHING_PROMPT = """You are a helpful tutor assisting a student with their homework. This is a matching quiz from an online course. Your job is to match each numbered item to its correct description from the given options.

{context_block}
Question: {question}

Items to match:
{items_block}

For each item, reply with the item number and the EXACT text of the correct option (copied exactly from the options list).
Reply in this format only, one match per line:
1: [exact option text]
2: [exact option text]
3: [exact option text]
4: [exact option text]

IMPORTANT: Use the EXACT option text as shown above. Do not paraphrase or abbreviate. Just match them correctly."""


# =============================================================================
# SOLVE_COMPLEX: Gemini 2.5 Pro Vision for Complex Screens
# =============================================================================

SOLVE_COMPLEX_PROMPT = """You are analyzing an educational quiz screenshot. Your job is to determine which answer(s) to select.

{context_block}

Question: {question}

Available answer options (exact text from the page):
{options_block}

Look at the screenshot carefully. Determine ALL correct answers to select.

IMPORTANT RULES:
- Reply with ONLY the letters of the correct answers, separated by commas
- Example for single answer: A
- Example for multiple answers: A, C
- If the question says "Choose 2 answers" or "Select all that apply", you MUST select multiple
- Use the screenshot to understand diagrams, tables, or visual context
- No explanation. Just the letters."""


async def _solve_complex_with_gemini(
    question: str,
    options: Optional[List[str]] = None,
    context_block: str = "",
    screenshot_b64: Optional[str] = None,
) -> dict:
    """
    Route complex screen to Claude CLI Opus 4.7 for vision-based solving.

    Per Jesse 2026-05-12, no Gemini path; function name retained for callers.
    Uses screenshot (if available) + question + options to determine answers.
    Returns in solve_checkbox format (selected list) for for_each compatibility.
    """
    try:
        letters = "ABCDEFGHIJ"
        if options:
            options_block = "\n".join(
                f"{letters[i]}) {opt}" for i, opt in enumerate(options) if i < len(letters)
            )
        else:
            options_block = "(No options provided - determine from screenshot)"

        prompt = SOLVE_COMPLEX_PROMPT.format(
            context_block=context_block,
            question=question,
            options_block=options_block,
        )

        raw_answer = await _solve_with_claude_cli_image(prompt, screenshot_b64)

        if not raw_answer:
            return {
                "success": False,
                "error": "Empty response from Claude CLI",
                "answer": "",
                "question_type": "solve_complex",
                "model": CLAUDE_CLI_MODEL,
            }

        # Parse response: same as solve_checkbox (letter-based)
        if options:
            letter_to_opt = {letters[i]: opt for i, opt in enumerate(options) if i < len(letters)}
            selected = []
            for part in raw_answer.replace(" ", "").split(","):
                part = part.strip().upper()
                if len(part) == 1 and part in letter_to_opt:
                    selected.append(letter_to_opt[part])
            # Fallback: try matching raw text to options
            if not selected:
                for opt in options:
                    if opt.lower() in raw_answer.lower():
                        selected.append(opt)
        else:
            selected = [raw_answer]

        logger.info(
            f"solve_complex: selected {len(selected)} answers: "
            f"{[s[:40] for s in selected]}"
        )

        return {
            "success": True,
            "answer": "complex_complete",
            "selected": selected,
            "raw_response": raw_answer,
            "question_type": "solve_complex",
            "model": CLAUDE_CLI_MODEL,
        }

    except Exception as e:
        logger.error(f"solve_complex Claude CLI error: {e}")
        return {
            "success": False,
            "error": f"solve_complex failed: {e}",
            "answer": "",
            "question_type": "solve_complex",
            "model": CLAUDE_CLI_MODEL,
        }


# =============================================================================
# SOLVE_MATCHING: Gemini 2.5 Pro Vision for Visual Matching Exercises
# =============================================================================

SOLVE_MATCHING_GEMINI_PROMPT = """You are analyzing an educational matching exercise. The screenshot shows a diagram or visual element with labeled parts that need to be matched to correct answers via dropdown menus.

{context_block}

Question: {question}

Items to match (each has a dropdown with options):
{items_block}

Look at the screenshot carefully. For each item, determine the correct answer from its available options.

IMPORTANT RULES:
- Reply with one match per line in this exact format: 1: [exact option text]
- Use the EXACT option text from the options list — do not paraphrase
- Use the screenshot to understand the visual context (diagrams, positions, labels)
- No explanation. Just the numbered matches."""


async def _solve_matching_with_gemini(
    question: str,
    items: list,
    context_block: str = "",
    screenshot_b64: str = None,
) -> dict:
    """
    Route matching exercise to Claude CLI (Opus 4.7) for vision-based solving.

    Per Jesse 2026-05-12, no Gemini path; function name retained for callers.
    Uses screenshot to understand visual context (diagrams, positions).
    Returns matches dict like the legacy Ollama/Gemini path.
    """
    try:
        items_block_parts = []
        for i, item in enumerate(items):
            label = item.get("label", f"Item {i+1}")
            item_options = item.get("options", [])
            opts_str = ", ".join(f'"{o}"' for o in item_options)
            items_block_parts.append(f"{i+1}. {label} — Options: [{opts_str}]")
        items_block = "\n".join(items_block_parts)

        prompt = SOLVE_MATCHING_GEMINI_PROMPT.format(
            context_block=context_block,
            question=question,
            items_block=items_block,
        )

        raw_answer = await _solve_with_claude_cli_image(prompt, screenshot_b64)

        if not raw_answer:
            return {
                "success": False,
                "error": "Empty response from Claude CLI",
                "answer": "",
                "question_type": "solve_matching",
                "model": CLAUDE_CLI_MODEL,
            }

        matches = parse_matching_response(raw_answer, items)
        logger.info(
            f"solve_matching: {len(matches)} matches from {len(items)} items"
        )

        return {
            "success": True,
            "answer": "matching_complete",
            "matches": matches,
            "raw_response": raw_answer,
            "question_type": "solve_matching",
            "model": CLAUDE_CLI_MODEL,
        }

    except Exception as e:
        logger.error(f"solve_matching Claude CLI error: {e}")
        return {
            "success": False,
            "error": f"solve_matching failed: {e}",
            "answer": "",
            "question_type": "solve_matching",
            "model": CLAUDE_CLI_MODEL,
        }


# =============================================================================
# GEMINI TEXT-ONLY: Primary model for factual Q&A
# =============================================================================

def _ensure_gemini():
    """REMOVED 2026-05-12. All LLM calls now route through Claude Opus 4.7
    via spark.tasks.claude_runner. This stub remains only to keep the module
    importable for any legacy caller; always returns False."""
    return False
    # vestigial body kept solely so the indentation parser inside this
    # try/except chain doesn't blow up; real callers have all been migrated.
    try:
        return True
    except Exception as e:
        logger.warning(f"Gemini setup failed: {e}")
        return False


async def _solve_with_gemini(prompt: str, screenshot_b64: str = None) -> Optional[str]:
    """
    Send a prompt (and optional screenshot) to Claude CLI Opus 4.7.

    Name retained for back-compat with all the existing callers in this
    file; per Jesse 2026-05-12 there is no Gemini path. Delegates to
    `_solve_with_claude_cli_image`, which handles the text-only fast
    path internally when screenshot_b64 is None.
    """
    return await _solve_with_claude_cli_image(prompt, screenshot_b64)


async def _solve_with_claude_cli(prompt: str, timeout: int = 120) -> Optional[str]:
    """Text-only prompt → Claude Opus 4.7 → answer text. Delegates to
    spark.tasks.claude_runner.call_claude_cli."""
    from spark.tasks.claude_runner import call_claude_cli, ClaudeCallError
    import asyncio as _aio

    def _do():
        try:
            raw, _meta = call_claude_cli(
                system_prompt="You are answering an educational quiz question. Reply with ONLY the answer in the exact format the user prompt requests — no preamble, no markdown fences.",
                user_message=prompt,
                timeout_s=timeout,
            )
            return raw
        except ClaudeCallError as e:
            logger.error(f"_solve_with_claude_cli: {e}")
            return None

    return await _aio.get_event_loop().run_in_executor(None, _do)


async def _solve_with_claude_cli_image(
    prompt: str,
    screenshot_b64: Optional[str],
    timeout: int = 180,
) -> Optional[str]:
    """Prompt + optional screenshot → Claude Opus 4.7 → answer text. The
    runner writes screenshot_b64 to a temp file, tells Claude to Read it,
    and verifies via num_turns that Read was actually invoked."""
    from spark.tasks.claude_runner import call_claude_cli, ClaudeCallError
    import asyncio as _aio

    def _do():
        try:
            raw, _meta = call_claude_cli(
                system_prompt="You are answering an educational quiz question using both the screenshot and the prompt below. Reply with ONLY the answer in the exact format the prompt requests — no preamble, no markdown fences.",
                user_message=prompt,
                screenshot_b64=screenshot_b64,
                timeout_s=timeout,
                require_screenshot_read=bool(screenshot_b64),
            )
            return raw
        except ClaudeCallError as e:
            logger.error(f"_solve_with_claude_cli_image: {e}")
            return None

    return await _aio.get_event_loop().run_in_executor(None, _do)


# =============================================================================
# ACTION SEQUENCE BUILDER
# =============================================================================

def build_action_sequence(
    answer: str,
    text_response: str,
    screen_config: dict,
    selected: list = None,
) -> list:
    """
    Build action_sequence from answer + screen YAML config.

    Uses answer_click, text_field, submit configs from YAML to construct
    the exact sequence of primitives Mac should execute.

    Args:
        answer: The selected answer text (for radio button click)
        text_response: Text to type into text field (empty if not needed)
        screen_config: Dict with answer_click, text_field, submit from YAML
        selected: List of selected options for checkbox multi-click

    Returns:
        List of action dicts Mac can execute in order
    """
    sequence = []

    answer_click = screen_config.get("answer_click", {})

    # Step 1a: Multi-select checkboxes (solve_checkbox)
    if selected and answer_click:
        for sel_text in selected:
            sequence.append({
                "type": "click",
                "target": sel_text,
                "target_role": answer_click.get("target_role", "AXCheckBox"),
                "strategy": answer_click.get("strategy", "mouse_click"),
                "match_mode": answer_click.get("match_mode", "exact"),
                "post_delay": answer_click.get("post_delay", 0.5),
            })

    # Step 1b: Single-select radio (solve_choice)
    elif answer and answer_click:
        sequence.append({
            "type": "click",
            "target": answer,
            "target_role": answer_click.get("target_role", "AXRadioButton"),
            "strategy": answer_click.get("strategy", "mouse_click"),
            "match_mode": answer_click.get("match_mode", "exact"),
            "post_delay": answer_click.get("post_delay", 0.5),
        })

    # Step 2: Type text response (if text field exists and we have text)
    text_field_config = screen_config.get("text_field", {})
    if text_response and text_field_config:
        sequence.append({
            "type": "type_text",
            "target": "",
            "target_role": text_field_config.get("target_role", "AXTextArea"),
            "text": text_response,
            "focus_strategy": text_field_config.get("focus_strategy", "mouse_click"),
            "post_delay": text_field_config.get("post_delay", 0.3),
        })

    # Step 3: Click Submit
    submit_config = screen_config.get("submit", {})
    if submit_config:
        sequence.append({
            "type": "click",
            "target": submit_config.get("target", "Submit"),
            "target_role": submit_config.get("target_role", "AXButton"),
            "strategy": submit_config.get("strategy", "mouse_click"),
            "match_mode": submit_config.get("match_mode", "exact"),
            "post_delay": submit_config.get("post_delay", 0.0),
        })

    return sequence


# =============================================================================
# CORE FUNCTION
# =============================================================================

async def generate_answer(
    question: str,
    question_type: str,
    options: Optional[List[str]] = None,
    context: Optional[List[str]] = None,
    image_descriptions: Optional[List[str]] = None,
    has_text_field: bool = False,
    screen_config: Optional[Dict] = None,
    items: Optional[List[Dict]] = None,
    screenshot_b64: Optional[str] = None,
) -> dict:
    """
    Generate answer for educational question.

    When screen_config is provided, returns action_sequence
    that Mac can execute as a dumb sequence of primitives.

    Args:
        question: The question text
        question_type: "solve_choice" (multiple choice) or "solve" (text input)
        options: Answer options for solve_choice (exact button text from Mac)
        context: Relevant content from SQLite KB
        image_descriptions: VLM descriptions of diagrams/equations on screen
        has_text_field: True if quiz has both radio buttons AND text area
        screen_config: Dict with answer_click, text_field, submit from YAML
                       When provided, response includes action_sequence

    Returns:
        {
            "success": True,
            "answer": "exact text to click",
            "text_response": "reflection text" (only if has_text_field),
            "action_sequence": [...] (only if screen_config provided),
            "question_type": "solve_choice" | "solve",
            "model": "llama3.1:8b"
        }
    """
    # Build context block from KB + image descriptions
    context_parts = []
    if context:
        context_parts.append("Reference material:\n" + "\n".join(context))
    if image_descriptions:
        context_parts.append("Visual content on screen:\n" + "\n".join(image_descriptions))

    context_block = "\n\n".join(context_parts) if context_parts else "No reference material available."

    # =========================================================================
    # SOLVE_COMPLEX: Route to Gemini 2.5 Pro (vision) instead of Ollama
    # =========================================================================
    if question_type == "solve_complex":
        return await _solve_complex_with_gemini(
            question=question,
            options=options,
            context_block=context_block,
            screenshot_b64=screenshot_b64,
        )

    # =========================================================================
    # SOLVE_MATCHING + SCREENSHOT: Route to Gemini (vision needed for diagrams)
    # =========================================================================
    if question_type == "solve_matching" and items and screenshot_b64:
        logger.info("solve_matching: screenshot present, routing to Gemini")
        return await _solve_matching_with_gemini(
            question=question,
            items=items,
            context_block=context_block,
            screenshot_b64=screenshot_b64,
        )

    # Build prompt based on question type
    if question_type == "navigate" and items:
        # Navigation: pick first incomplete item from list
        # Filter out short/generic links (< 4 chars = icons, arrows, etc.)
        # and common footer patterns. Keep curriculum content.
        items_parts = []
        idx = 0
        for item in items:
            desc = item.get("popup_desc", item.get("description", ""))
            if len(desc) < 4:
                continue
            idx += 1
            items_parts.append(f"{idx}. {desc}")
        if len(items) != idx:
            logger.info(f"navigate: filtered {len(items)} items to {idx}")
        items_block = "\n".join(items_parts)

        prompt = NAVIGATE_PROMPT.format(items_block=items_block)

    elif question_type == "solve_assessment" and items:
        # Full assessment: items is a list of {type, question, options}
        q_parts = []
        for i, item in enumerate(items):
            q_type = item.get("type", "radio")
            q_text = item.get("question", f"Question {i+1}")
            opts = item.get("options", [])
            letters = "ABCDEFGHIJ"

            if q_type == "radio":
                opts_str = "\n".join(f"  {letters[j]}) {o}" for j, o in enumerate(opts) if j < len(letters))
                q_parts.append(f"Question {i} [RADIO - select ONE]:\n{q_text}\n{opts_str}")
            elif q_type == "checkbox":
                opts_str = "\n".join(f"  {letters[j]}) {o}" for j, o in enumerate(opts) if j < len(letters))
                q_parts.append(f"Question {i} [CHECKBOX - select ALL correct]:\n{q_text}\n{opts_str}")

        questions_block = "\n\n".join(q_parts)
        prompt = SOLVE_ASSESSMENT_PROMPT.format(
            context_block=context_block,
            questions_block=questions_block,
        )

    elif question_type == "solve_matching" and items:
        # Matching quiz: items with discovered dropdown options
        items_block_parts = []
        for i, item in enumerate(items):
            label = item.get("label", f"Item {i+1}")
            item_options = item.get("options", [])
            opts_str = ", ".join(f'"{o}"' for o in item_options)
            items_block_parts.append(f"{i+1}. {label} — Options: [{opts_str}]")
        items_block = "\n".join(items_block_parts)

        prompt = SOLVE_MATCHING_PROMPT.format(
            context_block=context_block,
            question=question,
            items_block=items_block,
        )
    elif question_type == "solve_checkbox":
        if not options:
            return {
                "success": False,
                "error": "solve_checkbox requires options list",
                "answer": "",
                "question_type": question_type,
                "model": "none"
            }

        letters = "ABCDEFGHIJ"
        options_block = "\n".join(
            f"{letters[i]}) {opt}" for i, opt in enumerate(options) if i < len(letters)
        )

        prompt = SOLVE_CHECKBOX_PROMPT.format(
            context_block=context_block,
            question=question,
            options_block=options_block
        )
    elif question_type == "solve_choice":
        if not options:
            return {
                "success": False,
                "error": "solve_choice requires options list",
                "answer": "",
                "question_type": question_type,
                "model": "none"
            }

        letters = "ABCDEFGHIJ"
        options_block = "\n".join(
            f"{letters[i]}) {opt}" for i, opt in enumerate(options) if i < len(letters)
        )

        # Use text-field-aware prompt if quiz has both radio + text area
        if has_text_field:
            prompt = SOLVE_CHOICE_WITH_TEXT_PROMPT.format(
                context_block=context_block,
                question=question,
                options_block=options_block
            )
        else:
            prompt = SOLVE_CHOICE_PROMPT.format(
                context_block=context_block,
                question=question,
                options_block=options_block
            )
    else:
        # solve (text input)
        prompt = SOLVE_TEXT_PROMPT.format(
            context_block=context_block,
            question=question
        )

    # =========================================================================
    # MODEL ROUTING: Claude CLI Opus 4.7 only (Jesse 2026-05-12: no Gemini).
    # The legacy `_solve_with_gemini` name is preserved as a thin shim that
    # delegates to `_solve_with_claude_cli_image`; rename can come later.
    # =========================================================================
    model_used = CLAUDE_CLI_MODEL
    raw_answer = ""

    llm_response = await _solve_with_gemini(prompt, screenshot_b64=screenshot_b64)
    if llm_response:
        raw_answer = llm_response
    # No fallback — if Claude CLI fails, return error. Caller decides.

    try:
        if not raw_answer:
            return {
                "success": False,
                "error": "Empty response from all models",
                "answer": "",
                "question_type": question_type,
                "model": model_used
            }

        # Parse answer and text_response
        answer = ""
        text_response = ""
        matches = {}

        if question_type == "navigate" and items:
            # Match LLM response to one of the item descriptions
            descriptions = [
                item.get("popup_desc", item.get("description", ""))
                for item in items
            ]
            answer = match_to_option(raw_answer, descriptions) if descriptions else raw_answer
            logger.info(f"Navigate: selected '{answer[:80]}'")
            return {
                "success": True,
                "answer": answer,
                "question_type": question_type,
                "model": model_used,
            }

        elif question_type == "solve_assessment" and items:
            # Parse JSON response for full assessment
            answers = parse_assessment_response(raw_answer, items)
            logger.info(f"Assessment: {len(answers)} answers parsed")
            if not answers:
                return {
                    "success": False,
                    "error": f"Failed to parse assessment response (got 0 answers from {len(items)} questions)",
                    "answer": "",
                    "raw_response": raw_answer,
                    "question_type": question_type,
                    "model": model_used,
                }
            return {
                "success": True,
                "answer": "assessment_complete",
                "answers": answers,
                "raw_response": raw_answer,
                "question_type": question_type,
                "model": model_used,
            }

        elif question_type == "solve_checkbox" and options:
            # Parse comma-separated letters: "A, C, D" → list of option texts
            letters = "ABCDEFGHIJ"
            letter_to_opt = {letters[i]: opt for i, opt in enumerate(options) if i < len(letters)}
            selected = []
            # Split by comma, strip, and map to options
            for part in raw_answer.replace(" ", "").split(","):
                part = part.strip().upper()
                if len(part) == 1 and part in letter_to_opt:
                    selected.append(letter_to_opt[part])
            # If no letters found, try matching raw text to options
            if not selected:
                for opt in options:
                    if opt.lower() in raw_answer.lower():
                        selected.append(opt)
            logger.info(f"Checkbox: selected {len(selected)} of {len(options)}: {[s[:40] for s in selected]}")
            result = {
                "success": True,
                "answer": "checkbox_complete",
                "selected": selected,
                "raw_response": raw_answer,
                "question_type": question_type,
                "model": model_used,
            }
            # Build action_sequence for checkbox if screen_config provided
            if screen_config:
                result["action_sequence"] = build_action_sequence(
                    answer="",
                    text_response="",
                    screen_config=screen_config,
                    selected=selected,
                )
            return result

        elif question_type == "solve_matching" and items:
            # Parse matching response: "1: option text\n2: option text\n..."
            matches = parse_matching_response(raw_answer, items)
            answer = "matching_complete"
            logger.info(f"Generated matches: {matches}")

            result = {
                "success": True,
                "answer": answer,
                "matches": matches,
                "raw_response": raw_answer,
                "question_type": question_type,
                "model": model_used,
            }
            return result

        elif question_type == "solve_choice" and options:
            if has_text_field:
                # Parse ANSWER: X / REFLECTION: Y format
                answer_raw, text_response = parse_choice_with_text(raw_answer)
                answer = match_to_option(answer_raw, options)
            else:
                answer = match_to_option(raw_answer, options)
        else:
            answer = raw_answer

        logger.info(f"Generated answer: {answer[:80]}")
        if text_response:
            logger.info(f"Generated text_response: {text_response[:80]}")

        result = {
            "success": True,
            "answer": answer,
            "question_type": question_type,
            "model": model_used
        }

        # Include text_response only if actually generated
        if text_response:
            result["text_response"] = text_response

        # Build action_sequence if screen_config provided
        if screen_config:
            result["action_sequence"] = build_action_sequence(
                answer=answer,
                text_response=text_response,
                screen_config=screen_config,
            )

        return result

    except Exception as e:
        return {
            "success": False,
            "error": str(e),
            "answer": "",
            "question_type": question_type,
            "model": model_used
        }


def parse_assessment_response(raw: str, items: list) -> list:
    """Parse JSON response from solve_assessment, matching to exact option texts."""
    answers = []

    # Try to parse JSON from response
    data = None
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        # Extract JSON from surrounding text
        start = raw.find("{")
        end = raw.rfind("}") + 1
        if start >= 0 and end > start:
            try:
                data = json.loads(raw[start:end])
            except json.JSONDecodeError:
                pass

    if not data or "answers" not in data:
        logger.error(f"Could not parse assessment response: {raw[:300]}")
        return []

    for ans in data["answers"]:
        q_idx = ans.get("question_index", -1)
        q_type = ans.get("type", "radio")

        if q_idx < 0 or q_idx >= len(items):
            continue

        item = items[q_idx]
        options = item.get("options", [])

        if q_type == "radio":
            selected_raw = ans.get("selected", "")
            matched = match_to_option(selected_raw, options) if options else selected_raw
            answers.append({"type": "radio", "selected": matched})
        elif q_type == "checkbox":
            selected_list = ans.get("selected", [])
            matched_list = []
            for sel in selected_list:
                matched = match_to_option(sel, options) if options else sel
                matched_list.append(matched)
            answers.append({"type": "checkbox", "selected": matched_list})

    return answers


def parse_choice_with_text(raw: str) -> tuple:
    """
    Parse LLM output for choice+text format.

    Expected: "ANSWER: A\nREFLECTION: My thoughts..."
    Returns: (answer_part, text_part)
    """
    answer_part = ""
    text_part = ""

    for line in raw.strip().split("\n"):
        line = line.strip()
        if line.upper().startswith("ANSWER:"):
            answer_part = line.split(":", 1)[1].strip()
        elif line.upper().startswith("REFLECTION:"):
            text_part = line.split(":", 1)[1].strip()

    # Fallback: if format wasn't followed, treat whole thing as answer
    if not answer_part:
        answer_part = raw.strip().split("\n")[0].strip()

    return answer_part, text_part


def parse_matching_response(raw: str, items: List[Dict]) -> dict:
    """
    Parse LLM matching response into {popup_desc: selected_option} dict.

    Expected format from LLM:
        1: Option text for item 1
        2: Option text for item 2
        ...

    Returns dict keyed by popup_desc (what Mac uses to find the AXPopUpButton).
    """
    matches = {}
    lines = raw.strip().split("\n")

    for line in lines:
        line = line.strip()
        if not line or ":" not in line:
            continue
        parts = line.split(":", 1)
        try:
            idx = int(parts[0].strip()) - 1  # 1-indexed to 0-indexed
        except ValueError:
            continue
        selected = parts[1].strip().strip('"\'')
        if idx < len(items):
            item = items[idx]
            popup_desc = item.get("popup_desc", "")
            label = item.get("label", "")
            item_options = item.get("options", [])
            # Match to exact option text (handles minor LLM variations)
            best = match_to_option(selected, item_options) if item_options else selected
            # Key by popup_desc (legacy handler) AND label (behavior tree)
            if popup_desc:
                matches[popup_desc] = best
            if label:
                matches[label] = best

    return matches


def match_to_option(raw_answer: str, options: List[str]) -> str:
    """
    Match LLM output to the closest option text.

    Handles: letter responses (A, B, C), exact text, substring, word overlap.
    """
    raw_lower = raw_answer.lower().strip().strip('"\'.-')
    letters = "abcdefghij"

    # Letter match first (A, B, C, etc.) - most reliable with numbered prompt
    if len(raw_lower) == 1 and raw_lower in letters:
        idx = letters.index(raw_lower)
        if idx < len(options):
            return options[idx]

    # Letter with parenthesis or period: "A)" or "A."
    if len(raw_lower) >= 2 and raw_lower[0] in letters and raw_lower[1] in ").]":
        idx = letters.index(raw_lower[0])
        if idx < len(options):
            return options[idx]

    # Exact match
    for opt in options:
        if opt.lower().strip() == raw_lower:
            return opt

    # No match — return raw answer with warning. No fuzzy matching.
    logger.warning(f"Could not match '{raw_answer}' to options (letter and exact match failed): {options}")
    return raw_answer
