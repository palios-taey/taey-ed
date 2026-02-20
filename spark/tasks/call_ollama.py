# STATUS: FROZEN - Bug-fixed from v7. Verified 2026-02-20. Do not modify.
"""
Answer generation for educational quiz questions.

Primary: Gemini 2.5 Pro API (cascades to Flash if Pro rate-limited).
Fallback: Claude CLI (haiku) when all Gemini models exhausted.
No local models (no Ollama).

Supports question types:
- solve_choice: Pick the correct option from multiple choices
- solve: Generate a text answer (fill-in-the-blank, short answer)
- solve_checkbox: Select all correct answers from options
- solve_complex: Complex screen → Gemini vision (screenshot + text)
- solve_matching: Match items to options → Gemini vision if screenshot
- solve_assessment: Full multi-question graded assessment
- navigate: Pick first incomplete item from a list

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
GEMINI_MODELS = ["gemini-2.5-pro", "gemini-2.5-flash"]
CLAUDE_CLI_MODEL = "haiku"  # Claude CLI fallback when Gemini rate-limited
# Flash Lite excluded — gives 1-char garbage for educational questions.
# Claude CLI (haiku) is a better fallback than Flash Lite.


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


NAVIGATE_PROMPT = """You are helping navigate an educational platform. Given a list of content items, identify the FIRST item that has NOT been completed yet.

CRITICAL RULES:
1. Videos and Articles MUST be completed BEFORE exercises/practice for the SAME topic. Never skip an incomplete video or article to do an exercise.
2. "Understand" items MUST be completed BEFORE "Apply" items for the SAME topic.
3. Ignore any "Up next for you!" recommendations - always follow curriculum order (top to bottom).
4. NEVER pick an item whose description starts with "completed" - those are already done.

Each item has a label (status text near the item) and a description (the item's link text).

Items:
{items_block}

COMPLETION CHECK - An item is DONE if ANY of these are true:
- Its description starts with "completed" (e.g., "completed Article The biosphere")
- Its label contains "Completed", "Mastery points", a checkmark, or a percentage score

An item is INCOMPLETE if:
- Its description does NOT start with "completed"
- Its label is empty, says "Not started", "Start", "unfamiliar", or has no completion indicator

Priority order for picking the FIRST incomplete item:
1. First incomplete Video (description starts with "Video")
2. First incomplete Article (description starts with "Article")
3. First incomplete "Understand" item
4. First incomplete "Apply" or "Practice" item
5. First incomplete UNIT link (description starts with "UNIT")

Reply with ONLY the exact DESCRIPTION text of the first incomplete item. Nothing else. Just the description text, copied exactly."""


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
# SOLVE_COMPLEX: Gemini 2.5 Flash Vision for Complex Screens
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
    Route complex screen to Gemini 2.5 Flash for vision-based solving.

    Uses screenshot (if available) + question + options to determine answers.
    Returns in solve_checkbox format (selected list) for for_each compatibility.
    """
    try:
        import base64
        import google.generativeai as genai
        from pathlib import Path

        # Load Gemini API key
        secrets_path = Path(__file__).parent.parent / "palios-taey-secrets.json"
        if not secrets_path.exists():
            return {
                "success": False,
                "error": "Gemini API key not configured (palios-taey-secrets.json missing)",
                "answer": "",
                "question_type": "solve_complex",
                "model": "gemini-2.5-flash"
            }

        import json as _json
        secrets = _json.loads(secrets_path.read_text())
        api_key = secrets.get("gemini_api_key", "")
        if not api_key:
            return {
                "success": False,
                "error": "Gemini API key empty in secrets file",
                "answer": "",
                "question_type": "solve_complex",
                "model": "gemini-2.5-flash"
            }

        genai.configure(api_key=api_key)

        # Build options block
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

        # Build content parts: prompt + optional screenshot
        content_parts = [prompt]
        if screenshot_b64:
            image_data = base64.b64decode(screenshot_b64)
            mime_type = "image/png" if image_data[:8] == b'\x89PNG\r\n\x1a\n' else "image/jpeg"
            content_parts.append({"mime_type": mime_type, "data": image_data})
            logger.info("solve_complex: sending screenshot + text to Gemini")
        else:
            logger.info("solve_complex: no screenshot, sending text only to Gemini")

        # Call Gemini
        raw_answer = ""

        for model_name in GEMINI_MODELS:
            try:
                model = genai.GenerativeModel(model_name)
                response = model.generate_content(content_parts)
                raw_answer = response.text.strip()
                logger.info(f"solve_complex: Gemini ({model_name}) raw='{raw_answer}'")
                break
            except Exception as e:
                if "429" in str(e) or "quota" in str(e).lower():
                    logger.warning(f"solve_complex: {model_name} rate limited, trying next")
                    continue
                raise

        if not raw_answer:
            # Gemini exhausted — fall back to Claude CLI (text-only, no vision)
            logger.warning("solve_complex: all Gemini models rate-limited → Claude CLI fallback")
            text_prompt = SOLVE_COMPLEX_PROMPT.format(
                context_block=context_block,
                question=question,
                options_block=options_block,
            )
            cli_answer = await _solve_with_claude_cli(text_prompt, timeout=60)
            if cli_answer:
                raw_answer = cli_answer
                logger.info(f"solve_complex: Claude CLI answered, len={len(raw_answer)}")
            else:
                return {
                    "success": False,
                    "error": "All models failed (Gemini rate-limited, Claude CLI failed)",
                    "answer": "",
                    "question_type": "solve_complex",
                    "model": "claude-cli-haiku"
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

        logger.info(f"solve_complex: selected {len(selected)} answers: {[s[:40] for s in selected]}")

        return {
            "success": True,
            "answer": "complex_complete",
            "selected": selected,
            "raw_response": raw_answer,
            "question_type": "solve_complex",
            "model": GEMINI_MODELS[0],
        }

    except Exception as e:
        logger.error(f"solve_complex Gemini error: {e}")
        return {
            "success": False,
            "error": f"Gemini solve_complex failed: {e}",
            "answer": "",
            "question_type": "solve_complex",
            "model": "gemini-2.5-flash"
        }


# =============================================================================
# SOLVE_MATCHING: Gemini 2.5 Flash Vision for Visual Matching Exercises
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
    Route matching exercise to Gemini 2.5 Flash for vision-based solving.

    Uses screenshot to understand visual context (diagrams, positions) that
    text-only Llama cannot interpret. Returns matches dict like Ollama path.
    """
    try:
        import base64
        import google.generativeai as genai
        from pathlib import Path

        secrets_path = Path(__file__).parent.parent / "palios-taey-secrets.json"
        if not secrets_path.exists():
            return {
                "success": False,
                "error": "Gemini API key not configured",
                "answer": "",
                "question_type": "solve_matching",
                "model": "gemini-2.5-flash"
            }

        import json as _json
        secrets = _json.loads(secrets_path.read_text())
        api_key = secrets.get("gemini_api_key", "")
        if not api_key:
            return {
                "success": False,
                "error": "Gemini API key empty",
                "answer": "",
                "question_type": "solve_matching",
                "model": "gemini-2.5-flash"
            }

        genai.configure(api_key=api_key)

        # Build items block
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

        content_parts = [prompt]
        if screenshot_b64:
            image_data = base64.b64decode(screenshot_b64)
            mime_type = "image/png" if image_data[:8] == b'\x89PNG\r\n\x1a\n' else "image/jpeg"
            content_parts.append({"mime_type": mime_type, "data": image_data})
            logger.info("solve_matching: sending screenshot + text to Gemini")

        raw_answer = ""

        for model_name in GEMINI_MODELS:
            try:
                model = genai.GenerativeModel(model_name)
                response = model.generate_content(content_parts)
                raw_answer = response.text.strip()
                logger.info(f"solve_matching: Gemini ({model_name}) raw='{raw_answer}'")
                break
            except Exception as e:
                if "429" in str(e) or "quota" in str(e).lower():
                    logger.warning(f"solve_matching: {model_name} rate limited, trying next")
                    continue
                raise

        if not raw_answer:
            return {
                "success": False,
                "error": "Empty response from Gemini",
                "answer": "",
                "question_type": "solve_matching",
                "model": "gemini-2.5-flash"
            }

        # Parse using same logic as Ollama path
        matches = parse_matching_response(raw_answer, items)
        logger.info(f"solve_matching Gemini: {len(matches)} matches from {len(items)} items")

        return {
            "success": True,
            "answer": "matching_complete",
            "matches": matches,
            "raw_response": raw_answer,
            "question_type": "solve_matching",
            "model": GEMINI_MODELS[0],
        }

    except Exception as e:
        logger.error(f"solve_matching Gemini error: {e}")
        return {
            "success": False,
            "error": f"Gemini solve_matching failed: {e}",
            "answer": "",
            "question_type": "solve_matching",
            "model": "gemini-2.5-flash"
        }


# =============================================================================
# GEMINI TEXT-ONLY: Primary model for factual Q&A
# =============================================================================

_gemini_configured = False
_gemini_api_key = None


def _ensure_gemini():
    """Load Gemini API key once. Returns True if available."""
    global _gemini_configured, _gemini_api_key
    if _gemini_configured:
        return _gemini_api_key is not None

    _gemini_configured = True
    try:
        from pathlib import Path
        secrets_path = Path(__file__).parent.parent / "palios-taey-secrets.json"
        if not secrets_path.exists():
            logger.warning("Gemini API key not found (palios-taey-secrets.json missing)")
            return False
        secrets = json.loads(secrets_path.read_text())
        _gemini_api_key = secrets.get("gemini_api_key", "")
        if not _gemini_api_key:
            logger.warning("Gemini API key empty in secrets file")
            return False
        import google.generativeai as genai
        genai.configure(api_key=_gemini_api_key)
        return True
    except Exception as e:
        logger.warning(f"Gemini setup failed: {e}")
        return False


async def _solve_with_gemini(prompt: str) -> Optional[str]:
    """
    Send a text prompt to Gemini and return raw response.

    Cascades: 2.5 Pro → 2.5 Flash → 2.5 Flash Lite.
    Returns None on failure so caller can fall back to Claude CLI.
    """
    if not _ensure_gemini():
        return None

    try:
        import google.generativeai as genai

        for model_name in GEMINI_MODELS:
            try:
                model = genai.GenerativeModel(model_name)
                response = model.generate_content(prompt)
                raw = response.text.strip()
                if raw and len(raw) >= 1:
                    logger.info(f"Gemini ({model_name}) answered, len={len(raw)}")
                    return raw
                else:
                    logger.warning(f"Gemini {model_name} empty/too-short response, trying next")
                    continue
            except Exception as e:
                if "429" in str(e) or "quota" in str(e).lower():
                    logger.warning(f"Gemini {model_name} rate limited, trying next")
                    continue
                logger.error(f"Gemini {model_name} error: {e}")
                return None

        logger.warning("All Gemini models exhausted or rate-limited → Claude CLI fallback")
        return None

    except Exception as e:
        logger.error(f"Gemini text error: {e}")
        return None


async def _solve_with_claude_cli(prompt: str, timeout: int = 60) -> Optional[str]:
    """
    Fallback: send prompt to Claude CLI when all Gemini models exhausted.

    Uses the Claude Code CLI installed on this machine. Strips CLAUDECODE env
    var to avoid nested-session issues.
    """
    try:
        proc = await asyncio.create_subprocess_exec(
            "env", "-u", "CLAUDECODE", "claude", "--print", "--model", "haiku",
            "-p", prompt,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        raw = stdout.decode().strip()
        if raw:
            logger.info(f"Claude CLI (haiku) answered, len={len(raw)}")
            return raw
        logger.warning(f"Claude CLI empty response, stderr={stderr.decode()[:200]}")
        return None
    except asyncio.TimeoutError:
        logger.error(f"Claude CLI timed out after {timeout}s")
        return None
    except Exception as e:
        logger.error(f"Claude CLI error: {e}")
        return None


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

    V8: When screen_config is provided, returns action_sequence
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
    # SOLVE_COMPLEX: Route to Gemini 2.5 Flash (vision) instead of Ollama
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
        items_parts = []
        for i, item in enumerate(items):
            label = item.get("label", "")
            desc = item.get("popup_desc", item.get("description", ""))
            items_parts.append(f"{i+1}. Label: \"{label}\" — Description: \"{desc}\"")
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
    # MODEL ROUTING: Gemini Pro (primary) → Claude CLI (fallback)
    # No local models. Gemini cascades: Pro → Flash → Flash Lite.
    # =========================================================================
    model_used = "gemini-2.5-pro"
    raw_answer = ""

    gemini_response = await _solve_with_gemini(prompt)
    if gemini_response:
        raw_answer = gemini_response
        model_used = "gemini-2.5-pro"

    # Claude CLI fallback when all Gemini models exhausted
    if not raw_answer:
        claude_response = await _solve_with_claude_cli(prompt)
        if claude_response:
            raw_answer = claude_response
            model_used = f"claude-{CLAUDE_CLI_MODEL}"

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
            # V8: Build action_sequence for checkbox if screen_config provided
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

        # V8: Build action_sequence if screen_config provided
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

    # Substring match - option contained in answer or vice versa
    for opt in options:
        opt_lower = opt.lower().strip()
        if opt_lower in raw_lower or raw_lower in opt_lower:
            return opt

    # Word overlap - pick option with most shared words
    raw_words = set(raw_lower.split())
    best_opt = None
    best_overlap = 0
    for opt in options:
        opt_words = set(opt.lower().strip().split())
        overlap = len(raw_words & opt_words)
        if overlap > best_overlap:
            best_overlap = overlap
            best_opt = opt
    if best_opt and best_overlap > 0:
        return best_opt

    # No match - return raw answer, Mac will handle mismatch
    logger.warning(f"Could not match '{raw_answer}' to options: {options}")
    return raw_answer
