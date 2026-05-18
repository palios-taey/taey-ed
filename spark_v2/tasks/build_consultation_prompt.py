"""
Deterministic Context Router for consultation prompts.

Decision tree architecture:
  STEP 1: Psychological anchor (safety, no pressure)
  STEP 2: View screenshot + tree, confirm archetype classification
  STEP 3: Archetype-specific recipe card (BT template + checklist)
  STEP 4: Platform context (RESEARCH.md guidance + warnings)
  STEP 5: Universal rules (handlers, strategies, timing, syntax)
  STEP 6: Respond via API

Total prompt: ~100-150 lines (down from 1630 lines of CLAUDE.md).
No Weaviate exemplar dependency — works Day 1 with only RESEARCH.md + tree + screenshot.
"""

import logging
from pathlib import Path

from .classify_archetype import classify_archetype

logger = logging.getLogger(__name__)

PROMPTS_DIR = Path(__file__).resolve().parent.parent / "prompts"
RECIPES_DIR = PROMPTS_DIR / "recipes"
WARNINGS_DIR = PROMPTS_DIR / "warnings"
AXIOMS_PATH = PROMPTS_DIR / "axioms.md"
PLATFORMS_DIR = Path(__file__).resolve().parent.parent / "platforms"
CONSULT_DIR = Path("/tmp/taey-ed-consult-v2")

# Archetype -> what to look for in RESEARCH.md
RESEARCH_GUIDANCE = {
    "TRANSITION": "Focus on: navigation flow, button labels, page transitions, completion indicators.",
    "CONTENT_LIST": "Focus on: course organization, content hierarchy, how items are listed, completion indicators.",
    "ASSESSMENT_RADIO": "Focus on: question types, answer submission flow, submit button labels, post-answer behavior.",
    "ASSESSMENT_CHECKBOX": "Focus on: multi-select question patterns, submit button labels, post-answer behavior.",
    "ASSESSMENT_TEXT": "Focus on: text input questions, answer submission flow, text field roles.",
    "ASSESSMENT_MATCHING": "Focus on: dropdown/matching exercises, popup behavior, menu interaction patterns.",
    "VIDEO": "Focus on: video player behavior, completion detection, what happens when video ends.",
    "UNKNOWN": "Skim the full document — focus on screen types and UI patterns to help classify.",
}


def _load_file(path: Path) -> str:
    """Load a prompt file. Returns empty string if missing."""
    try:
        if path.exists():
            return path.read_text(encoding="utf-8").strip()
    except Exception as e:
        logger.warning(f"Failed to load {path}: {e}")
    return ""


def build_consultation_prompt(
    consultation_id: str,
    platform: str,
    tree: dict,
    escalation_level: str = "spark_claude",
    spark_attempts: int = 0,
    reconsult_context: str = "",
    is_reconsultation: bool = False,
) -> str:
    """
    Build a focused consultation prompt using decision tree routing.

    Returns ~100-150 lines of staged, actionable instructions.
    No CLAUDE.md reference. No Weaviate dependency. Works Day 1.
    """
    # Classify archetype from the tree
    archetype, evidence = classify_archetype(tree)
    logger.info(f"Archetype: {archetype} (rule: {evidence.get('rule', 'none')})")

    # Load recipe card for this archetype
    recipe = _load_file(RECIPES_DIR / f"{archetype}.md")
    if not recipe:
        recipe = _load_file(RECIPES_DIR / "UNKNOWN.md")

    # Load universal axioms
    axioms = _load_file(AXIOMS_PATH)

    # Load platform warnings
    warnings = _load_file(WARNINGS_DIR / f"{platform}.md")
    if not warnings:
        warnings = _load_file(WARNINGS_DIR / "_default.md")

    # Research guidance based on archetype
    research_guidance = RESEARCH_GUIDANCE.get(archetype, RESEARCH_GUIDANCE["UNKNOWN"])

    # Assemble staged prompt
    sections = []
    consult_path = CONSULT_DIR / consultation_id

    # === STEP 1: PSYCHOLOGICAL ANCHOR ===
    sections.append(
        "=== YOUR ROLE ===\n"
        "You are a Safe Explorer mapping an educational screen.\n"
        "- ZERO pressure to rush. Certainty matters, speed does not.\n"
        "- \"I don't know\" is VALID output — escalate rather than guess.\n"
        "- You have exactly ONE job: look at this screen, build the right behavior tree, respond via API.\n"
        "- If something doesn't make sense, STOP and research it. Wrong answers waste more time than careful analysis."
    )

    # === STEP 2: VIEW & CLASSIFY ===
    step2 = (
        f"=== STEP 1: VIEW & CLASSIFY ===\n"
        f"Consultation: {consultation_id} | Platform: {platform}\n"
        f"Escalation: {escalation_level} (attempt {spark_attempts})\n\n"
        f"Files to read (in this order):\n"
        f"1. SCREENSHOT: {consult_path / 'screenshot.png'} — READ THIS FIRST. You CAN view images.\n"
        f"2. TREE: {consult_path / 'tree.json'} — focus on AXWebArea, skip browser chrome.\n"
        f"3. METADATA: {consult_path / 'metadata.json'}"
    )
    if is_reconsultation:
        step2 += f"\n4. BT DEBUG: {consult_path / 'bt_debug.log'} — shows what the PREVIOUS tree tried."

    step2 += (
        f"\n\nDetected archetype: **{archetype}**\n"
        f"Evidence: {evidence.get('rule', 'none')}\n"
        f"Confirm or override this classification after viewing the screenshot and tree."
    )

    if reconsult_context:
        step2 += f"\n\n{reconsult_context}"

    sections.append(step2)

    # === STEP 3: RECIPE CARD ===
    sections.append(f"=== STEP 2: BUILD THE BEHAVIOR TREE ({archetype}) ===\n{recipe}")

    # === STEP 4: PLATFORM CONTEXT ===
    step4 = f"=== STEP 3: PLATFORM CONTEXT ({platform}) ==="

    research_path = PLATFORMS_DIR / platform / "RESEARCH.md"
    if research_path.exists():
        step4 += (
            f"\n\nRead: {research_path}\n"
            f"{research_guidance}"
        )
    else:
        step4 += "\n\nNo RESEARCH.md exists for this platform. Use your best judgment."

    if warnings:
        step4 += f"\n\n{warnings}"

    sections.append(step4)

    # === STEP 5: UNIVERSAL RULES ===
    sections.append(f"=== STEP 4: RULES & SYNTAX ===\n{axioms}")

    return "\n\n".join(sections)
