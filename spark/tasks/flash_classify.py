"""
V21 Flash Classification — screen type + variant via Gemini Flash.

Screenshot only (no tree). Fast, cheap (~$0.002/call).
Returns type + variant so the router can look up a cached BT.

If knowledge.json exists for the platform, includes the variant list
as suggested options with an escape hatch for unseen screen types.
"""

import base64
import json
import logging
from pathlib import Path
from typing import Optional

logger = logging.getLogger("taey-ed")

# Base screen types (universal)
SCREEN_TYPES = {
    "VIDEO": "Video player screen — video is playing, paused, or finished",
    "ARTICLE": "Reading/article content — text to read, may have 'Mark as completed'",
    "EXERCISE": "Quiz/assessment — questions with answer options, text input, or start button",
    "TRANSITION": "Interstitial screen — congratulations, quiz landing, 'Continue'/'Next' button",
    "NAVIGATION": "Course overview/home — list of lessons, sidebar, progress indicators",
    "UNKNOWN": "None of the above — unfamiliar screen type",
}

# Common variants (platform-agnostic starting list)
DEFAULT_VARIANTS = [
    "VIDEO_UNSTARTED",
    "VIDEO_PLAYING",
    "VIDEO_COMPLETE",
    "ARTICLE_READING",
    "EXERCISE_RADIO",
    "EXERCISE_CHECKBOX",
    "EXERCISE_TEXT_INPUT",
    "EXERCISE_MATCHING",
    "EXERCISE_DROPDOWN",
    "EXERCISE_FREE_RESPONSE",
    "TRANSITION",
    "NAVIGATION",
    "UNKNOWN",
]


def _build_variant_list(platform: str) -> str:
    """Build variant list from knowledge.json if available, else defaults."""
    from spark.tasks.knowledge_loader import load_knowledge

    knowledge = load_knowledge(platform)
    variants = list(DEFAULT_VARIANTS)

    if knowledge:
        screen_types = knowledge.get("screen_types", {})
        for stype, info in screen_types.items():
            # Add subtypes as variants
            for sub in info.get("subtypes", []):
                name = sub.get("name", "")
                if name:
                    variant_name = f"{stype}_{name.upper()}"
                    if variant_name not in variants:
                        variants.append(variant_name)

    return "\n".join(f"  - {v}" for v in sorted(set(variants)))


def _build_prompt(platform: str) -> str:
    """Build the Flash classification prompt."""
    type_descriptions = "\n".join(
        f"  - {name}: {desc}" for name, desc in SCREEN_TYPES.items()
    )
    variant_list = _build_variant_list(platform)

    return f"""\
You are classifying a screen on {platform} (educational platform).

Look at the screenshot and identify:
1. The screen TYPE (what kind of content/interaction)
2. The screen VARIANT (specific subtype)

SCREEN TYPES:
{type_descriptions}

KNOWN VARIANTS (use one if it matches):
{variant_list}

If none of these variants match what you see, create a descriptive variant name
using the format TYPE_DESCRIPTION (e.g., EXERCISE_DRAG_DROP, TRANSITION_PAYMENT_GATE).

CRITICAL DISTINCTIONS:
- TRANSITION has a single advance button (Next, Continue, Start Quiz) — no content to interact with
- ARTICLE has readable text content — may have "Mark as completed"
- EXERCISE has questions/inputs that require answering before submitting
- If you see "Start Quiz", "Start Assessment", or "Begin" — that's TRANSITION, not EXERCISE
- VIDEO has a video player with play/pause controls or progress bar

Respond with ONLY this JSON (no markdown, no explanation):
{{"screen_type": "TYPE", "variant": "VARIANT", "confidence_note": "Brief reason"}}"""


def classify_screen_flash(
    screenshot_b64: str,
    platform: str,
) -> dict:
    """
    Classify screen via Gemini Flash (screenshot only).

    Returns:
        {
            "screen_type": "EXERCISE",
            "variant": "EXERCISE_RADIO",
            "confidence_note": "Multiple choice with 4 radio options visible"
        }
    """
    try:
        import google.generativeai as genai
        from .paths import SECRETS_PATH

        secrets_path = SECRETS_PATH
        if not secrets_path.exists():
            logger.error(f"flash_classify: Gemini API key not configured (secrets at {secrets_path} missing)")
            return _fallback("no_api_key")

        secrets = json.loads(secrets_path.read_text())
        api_key = secrets.get("gemini_api_key", "")
        if not api_key:
            logger.error("flash_classify: Gemini API key empty")
            return _fallback("api_key_empty")

        genai.configure(api_key=api_key)

        prompt = _build_prompt(platform)

        # Build content: prompt + screenshot
        content_parts = [prompt]
        try:
            image_data = base64.b64decode(screenshot_b64)
            mime_type = "image/png" if image_data[:8] == b'\x89PNG\r\n\x1a\n' else "image/jpeg"
            content_parts.append({"mime_type": mime_type, "data": image_data})
        except Exception as e:
            logger.error(f"flash_classify: screenshot decode failed: {e}")
            return _fallback("screenshot_decode_failed")

        model = genai.GenerativeModel("gemini-2.5-flash")
        logger.info(f"flash_classify: calling gemini-2.5-flash for {platform}")
        response = model.generate_content(content_parts)
        raw = response.text.strip()

        logger.info(f"flash_classify: response len={len(raw)}")

        # Parse JSON response
        if raw.startswith("```"):
            raw = raw.split("\n", 1)[1] if "\n" in raw else raw[3:]
            if raw.endswith("```"):
                raw = raw[:-3]
            raw = raw.strip()

        result = json.loads(raw)

        screen_type = result.get("screen_type", "UNKNOWN")
        variant = result.get("variant", screen_type)
        note = result.get("confidence_note", "")

        # Normalize: ensure variant starts with the screen_type
        if not variant.startswith(screen_type) and variant != screen_type:
            variant = f"{screen_type}_{variant}"

        logger.info(
            f"flash_classify: type={screen_type} variant={variant} note={note}"
        )

        return {
            "screen_type": screen_type,
            "variant": variant,
            "confidence_note": note,
        }

    except json.JSONDecodeError as e:
        logger.error(f"flash_classify: JSON parse failed: {e} raw={raw[:200]}")
        return _fallback("json_parse_failed")
    except Exception as e:
        logger.error(f"flash_classify: error: {e}")
        return _fallback(str(e))


def _fallback(reason: str) -> dict:
    """Return UNKNOWN when Flash fails. Caller should fall through to Pro."""
    return {
        "screen_type": "UNKNOWN",
        "variant": "UNKNOWN",
        "confidence_note": f"Flash classification failed: {reason}",
    }
