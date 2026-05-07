# STATUS: FROZEN - Proven in v7. Verified 2026-02-19. Do not modify.
"""
Gemini API image analysis for content extraction.

Phase 5: Extract text descriptions from lesson images/diagrams.
Spark provides COMPUTE only - Mac stores results locally.

Uses Gemini 2.5 Pro with model cascade for rate limit handling.
"""

import base64
import json
import struct
from pathlib import Path
from typing import Optional, Tuple

import google.generativeai as genai

# =============================================================================
# CONFIGURATION
# =============================================================================
from .paths import SECRETS_PATH
MIN_IMAGE_SIZE = 32  # Minimum image dimension

# Load API key
def _load_api_key() -> str:
    if SECRETS_PATH.exists():
        secrets = json.loads(SECRETS_PATH.read_text())
        return secrets.get("gemini_api_key", "")
    return ""

# Configure Gemini
_api_key = _load_api_key()
if _api_key:
    genai.configure(api_key=_api_key)

# Model cascade: Pro → Pro (single model, no fallback)
MODELS = {
    "primary": "gemini-2.5-pro",
    "heavy": "gemini-2.5-pro",
    "fallback": "gemini-2.5-pro",
}

EXTRACTION_PROMPT = """Extract from this educational screenshot:
1. ALL visible text (complete OCR) - preserve exact formatting, equations, numbers, symbols
2. Math equations in LaTeX format if present
3. Describe any diagrams, charts, or images
4. Identify the question being asked and answer choices if present

Return as JSON:
{
    "text_content": "all visible text exactly as written",
    "equations": ["LaTeX equations if any"],
    "diagrams": "description of visual elements",
    "question": "the question being asked",
    "answer_choices": ["list of choices if multiple choice"],
    "question_type": "multiple_choice | text_input | diagram | reading"
}"""


def get_image_dimensions(image_b64: str) -> Tuple[int, int]:
    """
    Extract image dimensions from base64-encoded PNG/JPEG.
    Returns: (width, height) or (0, 0) if cannot determine.
    """
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
    """
    Analyze image with Gemini API.

    Args:
        image_b64: Base64-encoded image (PNG/JPEG)
        purpose: Optional hint (unused now, kept for API compatibility)
        context: Optional course context

    Returns:
        {
            "success": True,
            "description": "Text description of image content",
            "content_type": "diagram|equation|text|photo|chart",
            "extracted": { full JSON response from Gemini }
        }
    """
    if not _api_key:
        return {
            "success": False,
            "error": "Gemini API key not configured",
            "description": "",
            "content_type": "unknown"
        }

    # Validate image dimensions
    width, height = get_image_dimensions(image_b64)
    if width > 0 and height > 0:
        if width < MIN_IMAGE_SIZE or height < MIN_IMAGE_SIZE:
            return {
                "success": False,
                "error": f"Image too small ({width}x{height}). Minimum {MIN_IMAGE_SIZE}x{MIN_IMAGE_SIZE}.",
                "description": "",
                "content_type": "unknown"
            }

    # Build prompt with context if available
    prompt = EXTRACTION_PROMPT
    if context:
        prompt += f"\n\nCourse context: {context}"

    # Decode image for Gemini
    image_data = base64.b64decode(image_b64)

    # Determine mime type
    if image_data[:8] == b'\x89PNG\r\n\x1a\n':
        mime_type = "image/png"
    else:
        mime_type = "image/jpeg"

    # Single model call — no cascade. Rate limit = fail loudly.
    try:
        model = genai.GenerativeModel(MODELS["primary"])

        response = model.generate_content(
            [
                prompt,
                {"mime_type": mime_type, "data": image_data}
            ],
            generation_config={"response_mime_type": "application/json"}
        )

        # Parse JSON response
        try:
            extracted = json.loads(response.text)
        except json.JSONDecodeError:
            extracted = {"text_content": response.text}

        # Build description from extracted content
        description_parts = []
        if extracted.get("text_content"):
            description_parts.append(f"Text: {extracted['text_content']}")
        if extracted.get("question"):
            description_parts.append(f"Question: {extracted['question']}")
        if extracted.get("diagrams"):
            description_parts.append(f"Visual: {extracted['diagrams']}")
        if extracted.get("equations"):
            description_parts.append(f"Equations: {', '.join(extracted['equations'])}")

        description = "\n".join(description_parts) if description_parts else response.text

        # Classify content type
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
            "model_used": MODELS["primary"]
        }

    except Exception as e:
        return {
            "success": False,
            "error": str(e),
            "description": "",
            "content_type": "unknown"
        }
