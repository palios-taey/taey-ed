# STATUS: FROZEN - Proven in v7. Verified 2026-02-19. Do not modify.
"""
Behavior Tree Helpers - Tree walking utilities for menu discovery and element finding.

These functions walk accessibility tree structures to find specific patterns
like popup menus, labels, assessment questions, etc.
"""

import logging
from typing import Optional

from app.tasks.bt_core import btlog

logger = logging.getLogger("taey-ed")


# =========================================================================
# Tree Walking Helpers
# =========================================================================

def _find_menu_subtree(tree: dict) -> Optional[dict]:
    """
    Find AXMenu element that is NOT under AXMenuBar.
    When a popup is clicked, it creates an AXMenu with AXMenuItems.
    The app menu bar also has AXMenu children - we skip those.
    """
    found = [None]

    def walk(node: dict, parent_role: str = ""):
        if found[0] is not None:
            return
        if not isinstance(node, dict):
            return
        role = node.get("role", "")
        if role == "AXMenu" and parent_role != "AXMenuBar":
            found[0] = node
            return
        for child in node.get("children", []):
            walk(child, parent_role=role)

    walk(tree)
    return found[0]


def _extract_menu_items(tree: dict, role: str) -> list:
    """
    Extract menu item text from tree, stripping ' selected' suffix.
    Chrome appends ' selected' to the currently-selected option.
    """
    items = []

    def walk(node: dict):
        if not isinstance(node, dict):
            return
        if node.get("role") == role:
            text = node.get("title") or node.get("value") or node.get("description") or ""
            text = str(text).strip()
            if text.endswith(" selected"):
                text = text[:-9].strip()
            # Strip decorative unicode arrows
            text = text.replace("\u2192", "").replace("→", "").strip()
            if text and text not in items:
                items.append(text)
        for child in node.get("children", []):
            walk(child)

    walk(tree)
    return items


def _find_preceding_label(tree: dict, popup_desc: str) -> str:
    """
    Find AXStaticText label that precedes a popup button in tree order.
    Associates each popup with its question item (e.g., "Canvas", "Voice Mode").
    """
    last_label = ""
    found = [False]

    def walk(node: dict):
        if found[0]:
            return
        if not isinstance(node, dict):
            return

        role = node.get("role", "")
        text = node.get("title") or node.get("value") or node.get("description") or node.get("name") or ""
        text = str(text).strip()

        if role == "AXStaticText" and text and len(text) > 1:
            nonlocal last_label
            # Strip decorative arrows and extra whitespace
            text = text.replace("\u2192", "").replace("→", "").strip()
            if text:
                last_label = text

        if role == "AXPopUpButton":
            desc = node.get("description") or node.get("title") or node.get("value") or ""
            desc = str(desc).strip()
            if popup_desc.lower() in desc.lower():
                found[0] = True
                return

        for child in node.get("children", []):
            walk(child)

    walk(tree)
    return last_label


def _find_elements_by_role(node: dict, target_role: str) -> list:
    """Recursively find all elements with a given role within a subtree."""
    results = []
    if not isinstance(node, dict):
        return results
    if node.get("role") == target_role and node.get("name"):
        results.append(node.get("name", ""))
        return results
    for child in node.get("children", []):
        results.extend(_find_elements_by_role(child, target_role))
    return results


def _find_assessment_questions(web_area: dict) -> list:
    """
    Find assessment questions in two modes:

    Mode 1 (structured): "Question N ..." AXGroup containers (practice quizzes).
    Mode 2 (simple): No containers — find checkboxes/radios directly in tree
      and use nearby question text (in-video quiz modals, simple quiz pages).

    Each question has either AXRadioGroup (single choice) or AXCheckBox (multi choice).
    Returns list of {type, question, options} dicts in order.
    """
    import re
    questions = []

    # --- Mode 1: Structured "Question N" containers ---
    def walk_structured(node, depth=0):
        if not isinstance(node, dict) or depth > 20:
            return
        role = node.get("role", "")
        name = node.get("name", "")

        if role == "AXGroup" and name.startswith("Question "):
            q_match = re.match(r"Question\s+\d+\s+(.*)", name)
            q_text = q_match.group(1) if q_match else name

            radio_options = _find_elements_by_role(node, "AXRadioButton")
            cb_options = []
            for cb_name in _find_elements_by_role(node, "AXCheckBox"):
                if cb_name and not cb_name.startswith("I, ") and cb_name not in ("Like", "Dislike"):
                    cb_options.append(cb_name)

            if radio_options:
                questions.append({
                    "type": "radio",
                    "question": q_text,
                    "options": radio_options,
                })
                btlog(f"  Q{len(questions)}: RADIO '{q_text[:60]}' options={len(radio_options)}")
            elif cb_options:
                questions.append({
                    "type": "checkbox",
                    "question": q_text,
                    "options": cb_options,
                })
                btlog(f"  Q{len(questions)}: CHECKBOX '{q_text[:60]}' options={len(cb_options)}")
            else:
                btlog(f"  Q?: UNKNOWN '{q_text[:60]}' (no radio/checkbox found)")
            return

        for child in node.get("children", []):
            walk_structured(child, depth + 1)

    walk_structured(web_area)

    if questions:
        btlog(f"  Mode 1 (structured): found {len(questions)} questions")
        return questions

    # --- Mode 2: Simple quiz (no "Question N" containers) ---
    # Find all checkboxes and radios directly, group as single question
    btlog("  Mode 1 found nothing, trying Mode 2 (simple quiz)...")

    all_radios = _find_elements_by_role(web_area, "AXRadioButton")
    all_checkboxes = []
    for cb_name in _find_elements_by_role(web_area, "AXCheckBox"):
        # Exclude non-answer checkboxes (honor code, like/dislike, etc.)
        if cb_name and not cb_name.startswith("I, ") and cb_name not in ("Like", "Dislike"):
            all_checkboxes.append(cb_name)

    # Find question text: look for text containing "?" in the tree
    question_text = ""
    def find_question_text(node, depth=0):
        nonlocal question_text
        if question_text or not isinstance(node, dict) or depth > 15:
            return
        role = node.get("role", "")
        name = node.get("name", "")
        if role == "AXStaticText" and "?" in name and len(name) > 10:
            question_text = name
            return
        # Also check AXGroup names that contain "?"
        if role == "AXGroup" and "?" in name and len(name) > 10:
            question_text = name
            return
        for child in node.get("children", []):
            find_question_text(child, depth + 1)

    find_question_text(web_area)

    if all_checkboxes and question_text:
        questions.append({
            "type": "checkbox",
            "question": question_text,
            "options": all_checkboxes,
        })
        btlog(f"  Mode 2: CHECKBOX '{question_text[:60]}' options={len(all_checkboxes)}")
    elif all_radios and question_text:
        questions.append({
            "type": "radio",
            "question": question_text,
            "options": all_radios,
        })
        btlog(f"  Mode 2: RADIO '{question_text[:60]}' options={len(all_radios)}")
    elif all_checkboxes:
        questions.append({
            "type": "checkbox",
            "question": "Select the correct answers",
            "options": all_checkboxes,
        })
        btlog(f"  Mode 2: CHECKBOX (no question text found) options={len(all_checkboxes)}")
    elif all_radios:
        questions.append({
            "type": "radio",
            "question": "Select the correct answer",
            "options": all_radios,
        })
        btlog(f"  Mode 2: RADIO (no question text found) options={len(all_radios)}")

    if questions:
        btlog(f"  Mode 2 (simple): found {len(questions)} questions")

    return questions


def _find_web_area(tree: dict) -> dict:
    """Find AXWebArea subtree for scoping."""
    if not isinstance(tree, dict):
        return tree
    if tree.get("role") == "AXWebArea":
        return tree
    for child in tree.get("children", []):
        result = _find_web_area(child)
        if isinstance(result, dict) and result.get("role") == "AXWebArea":
            return result
    return tree
