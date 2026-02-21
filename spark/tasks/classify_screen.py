"""
Screen classification via Gemini API.

Step A of the 2-step classification process (REBUILD_PLAN.md Part 10).
Sends tree + screenshot to Gemini and asks: "What type of screen is this?"

Returns one of 6 universal categories:
  NAVIGATION, VIDEO, ARTICLE, EXERCISE, TRANSITION, UNKNOWN

Does NOT build BTs. That's build_screen_bt.py (Step B).
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
        model = genai.GenerativeModel("gemini-2.5-flash")
        response = model.generate_content(content_parts)
        raw = response.text.strip()

        logger.info(f"classify_screen: Gemini response len={len(raw)}")

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
