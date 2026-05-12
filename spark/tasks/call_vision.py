"""Image content extraction via Claude Opus 4.7.

Given an image, returns a structured description (text, equations, diagrams,
question, choices). Used by /api/v1/extract_image when something downstream
needs to describe a screenshot or cropped region.

History: originally targeted Gemini 2.5 Pro. Swapped to Claude Opus 4.7 on
2026-05-12 per Jesse — no Gemini path anywhere.
"""

import base64
import json
import struct
from typing import Optional, Tuple

from spark.tasks.claude_runner import call_claude_cli, ClaudeCallError

MIN_IMAGE_SIZE = 32  # Minimum image dimension to bother analyzing

EXTRACTION_PROMPT = """Extract from this educational screenshot:
1. ALL visible text (complete OCR) - preserve exact formatting, equations, numbers, symbols
2. Math equations in LaTeX format if present
3. Describe any diagrams, charts, or images
4. Identify the question being asked and answer choices if present

Return as JSON ONLY (no markdown fences, no commentary):
{
    "text_content": "all visible text exactly as written",
    "equations": ["LaTeX equations if any"],
    "diagrams": "description of visual elements",
    "question": "the question being asked",
    "answer_choices": ["list of choices if multiple choice"],
    "question_type": "multiple_choice | text_input | diagram | reading"
}"""


def get_image_dimensions(image_b64: str) -> Tuple[int, int]:
    """Extract image dimensions from base64-encoded PNG/JPEG.
    Returns: (width, height) or (0, 0) if cannot determine."""
    try:
        image_data = base64.b64decode(image_b64)

        # PNG: signature + IHDR chunk contains dimensions
        if image_data[:8] == b'\x89PNG\r\n\x1a\n':
            width = struct.unpack('>I', image_data[16:20])[0]
            height = struct.unpack('>I', image_data[20:24])[0]
            return (width, height)

        # JPEG: search for SOF0/SOF2 marker
        if image_data[:2] == b'\xff\xd8':
            i = 2
            while i < len(image_data) - 8:
                if image_data[i] == 0xff:
                    marker = image_data[i+1]
                    if marker in (0xC0, 0xC2):
                        height = struct.unpack('>H', image_data[i+5:i+7])[0]
                        width = struct.unpack('>H', image_data[i+7:i+9])[0]
                        return (width, height)
                    if marker != 0:
                        length = struct.unpack('>H', image_data[i+2:i+4])[0]
                        i += 2 + length
                    else:
                        i += 1
                else:
                    i += 1

        return (0, 0)
    except Exception:
        return (0, 0)


async def extract_image_content(
    image_b64: str,
    purpose: Optional[str] = None,
    context: Optional[str] = None
) -> dict:
    """Analyze image content with Claude Opus 4.7 vision.

    Args:
        image_b64: Base64-encoded image (PNG/JPEG)
        purpose: Optional hint (unused now, kept for API compatibility)
        context: Optional course context

    Returns:
        {
            "success": True,
            "description": "Text description of image content",
            "content_type": "diagram|equation|text|photo|chart",
            "extracted": { full JSON response from the model }
        }
    """
    width, height = get_image_dimensions(image_b64)
    if width > 0 and height > 0:
        if width < MIN_IMAGE_SIZE or height < MIN_IMAGE_SIZE:
            return {
                "success": False,
                "error": f"Image too small ({width}x{height}). Minimum {MIN_IMAGE_SIZE}x{MIN_IMAGE_SIZE}.",
                "description": "",
                "content_type": "unknown",
            }

    prompt = EXTRACTION_PROMPT
    if context:
        prompt += f"\n\nCourse context: {context}"

    import asyncio
    loop = asyncio.get_event_loop()

    def _do():
        try:
            return call_claude_cli(
                system_prompt="You extract structured information from educational screenshots. Reply with ONLY the JSON object — no markdown fences, no commentary.",
                user_message=prompt,
                screenshot_b64=image_b64,
                require_screenshot_read=True,
            )
        except ClaudeCallError as e:
            return None, {"error": str(e)}

    try:
        raw, meta = await loop.run_in_executor(None, _do)
    except Exception as e:
        return {
            "success": False,
            "error": str(e),
            "description": "",
            "content_type": "unknown",
        }

    if raw is None:
        return {
            "success": False,
            "error": meta.get("error", "Claude call failed"),
            "description": "",
            "content_type": "unknown",
        }

    raw = raw.strip()
    if raw.startswith("```"):
        raw = raw.split("\n", 1)[1] if "\n" in raw else raw[3:]
        if raw.endswith("```"):
            raw = raw[:-3]
        raw = raw.strip()

    try:
        extracted = json.loads(raw)
    except json.JSONDecodeError:
        extracted = {"text_content": raw}

    description_parts = []
    if extracted.get("text_content"):
        description_parts.append(f"Text: {extracted['text_content']}")
    if extracted.get("question"):
        description_parts.append(f"Question: {extracted['question']}")
    if extracted.get("diagrams"):
        description_parts.append(f"Visual: {extracted['diagrams']}")
    if extracted.get("equations"):
        description_parts.append(f"Equations: {', '.join(extracted['equations'])}")

    description = "\n".join(description_parts) if description_parts else raw

    content_type = extracted.get("question_type", "unknown")
    if content_type == "unknown":
        if extracted.get("equations"):
            content_type = "equation"
        elif extracted.get("diagrams"):
            content_type = "diagram"
        else:
            content_type = "text"

    return {
        "success": True,
        "description": description,
        "content_type": content_type,
        "extracted": extracted,
        "model_used": meta.get("model", "claude-opus-4-7"),
    }
