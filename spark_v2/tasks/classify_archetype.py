"""
Heuristic screen archetype classifier.

Deterministic (no LLM), fast (<10ms).
Analyzes the accessibility tree's AXWebArea subtree only (skips browser chrome).
Returns one of 8 archetypes used to select the recipe card for consultation prompts.
"""

from collections import Counter

# The 8 archetypes
TRANSITION = "TRANSITION"
CONTENT_LIST = "CONTENT_LIST"
ASSESSMENT_RADIO = "ASSESSMENT_RADIO"
ASSESSMENT_CHECKBOX = "ASSESSMENT_CHECKBOX"
ASSESSMENT_TEXT = "ASSESSMENT_TEXT"
ASSESSMENT_MATCHING = "ASSESSMENT_MATCHING"
VIDEO = "VIDEO"
UNKNOWN = "UNKNOWN"

# Roles that indicate browser chrome — skip these subtrees
CHROME_ROLES = {"AXMenuBar", "AXMenuBarItem", "AXMenu", "AXToolbar"}

# Video player signals — must be specific to actual players, not sidebar links listing videos
# "video" alone is too broad (Khan Academy sidebars say "Video Worked example: ...")
VIDEO_PLAYER_KEYWORDS = {
    "video player", "youtube", "play video", "vimeo player", "wistia",
    "pause video", "video playback", "playback speed", "video progress",
    "media player", "video timeline",
}
VIDEO_PLAYER_ROLES = {"AXVideo", "AXMediaTimeline"}

# Post-answer feedback signals — if present, screen is showing results, not active assessment
# These are button/link texts that appear AFTER answering, indicating feedback state
POST_ANSWER_KEYWORDS = {"next question", "show summary", "keep going"}


def classify_archetype(tree: dict) -> tuple[str, dict]:
    """
    Classify accessibility tree into one of 8 archetypes.

    Args:
        tree: Full accessibility tree dict from Mac capture_tree

    Returns:
        (archetype_name, evidence_dict)
        evidence_dict has the role counts and text signals that
        led to the classification (for debugging/logging).
    """
    # Step 1: Find AXWebArea subtree (skip browser chrome)
    web_area = _find_web_area(tree)
    if web_area is None:
        return UNKNOWN, {"reason": "no_web_area_found"}

    # Step 2: Count roles and detect text signals in web_area only
    role_counts, text_signals = _analyze_web_area(web_area)

    evidence = {
        "role_counts": dict(role_counts.most_common(10)),
        "text_signals": text_signals,
    }

    # Step 3: Classification rules (most specific first)

    # VIDEO: actual video player element or specific player keywords
    if text_signals.get("has_video_player"):
        evidence["rule"] = "video_player_detected"
        return VIDEO, evidence

    # Post-answer feedback override: if "next question"/"show summary"/"keep going"
    # is detected, this is a feedback/results screen — skip assessment rules entirely.
    # These screens retain checkbox/radio elements from the previous question but
    # are really TRANSITION screens.
    is_post_answer = text_signals.get("has_post_answer", False)

    # ASSESSMENT rules — only if NOT a post-answer feedback screen
    if not is_post_answer:
        # ASSESSMENT_MATCHING: multiple popups/comboboxes = dropdown matching
        popup_count = role_counts.get("AXPopUpButton", 0)
        combo_count = role_counts.get("AXComboBox", 0)
        if popup_count >= 2 or combo_count >= 2:
            evidence["rule"] = f"popup={popup_count} combo={combo_count}"
            return ASSESSMENT_MATCHING, evidence

        # ASSESSMENT_RADIO: radio buttons present (3+ to avoid minor UI radios)
        radio_count = role_counts.get("AXRadioButton", 0)
        if radio_count >= 3:
            evidence["rule"] = f"radio={radio_count}"
            return ASSESSMENT_RADIO, evidence

        # ASSESSMENT_CHECKBOX: checkboxes present (3+ to avoid minor UI toggles)
        checkbox_count = role_counts.get("AXCheckBox", 0)
        if checkbox_count >= 3:
            evidence["rule"] = f"checkbox={checkbox_count}"
            return ASSESSMENT_CHECKBOX, evidence

        # ASSESSMENT_TEXT: text input field + question-like context
        textarea_count = role_counts.get("AXTextArea", 0)
        textfield_count = role_counts.get("AXTextField", 0)
        if (textarea_count >= 1 or textfield_count >= 1) and text_signals.get("has_question"):
            evidence["rule"] = f"textarea={textarea_count} textfield={textfield_count} has_question"
            return ASSESSMENT_TEXT, evidence

    # CONTENT_LIST: many links suggest a content list/navigation page
    # Threshold 30+ to avoid false positives from KA sidebar (18-25 links)
    # Genuine content list screens (COURSE_OVERVIEW, UNIT_OVERVIEW) have 100+ links
    link_count = role_counts.get("AXLink", 0)
    if link_count >= 30:
        evidence["rule"] = f"links={link_count}"
        return CONTENT_LIST, evidence

    # TRANSITION: buttons or links present but no assessment/video signals
    button_count = role_counts.get("AXButton", 0)
    if button_count >= 1 or link_count >= 1:
        evidence["rule"] = f"buttons={button_count} links={link_count}"
        return TRANSITION, evidence

    return UNKNOWN, evidence


def _find_web_area(tree: dict) -> dict | None:
    """DFS to find first AXWebArea node (skips browser chrome)."""
    if tree.get("role") == "AXWebArea":
        return tree
    for child in tree.get("children", []):
        result = _find_web_area(child)
        if result is not None:
            return result
    return None


def _analyze_web_area(web_area: dict) -> tuple[Counter, dict]:
    """
    Count roles and detect text signals within AXWebArea only.
    Skips browser chrome subtrees (AXMenuBar, AXToolbar).
    """
    role_counts: Counter = Counter()
    text_signals = {
        "has_question": False,
        "has_video_player": False,
        "has_post_answer": False,
    }

    stack = [web_area]
    while stack:
        node = stack.pop()
        if not isinstance(node, dict):
            continue

        role = node.get("role", "")

        # Skip browser chrome subtrees entirely
        if role in CHROME_ROLES:
            continue

        if role:
            role_counts[role] += 1

        # Video player detection — role-based (most reliable)
        if role in VIDEO_PLAYER_ROLES:
            text_signals["has_video_player"] = True

        # Text signal detection
        for field in ("name", "value", "title", "description"):
            val = node.get(field)
            if not val or not isinstance(val, str):
                continue
            val_lower = val.lower()
            if "?" in val_lower and len(val_lower) > 5:
                text_signals["has_question"] = True
            # Only match specific video player phrases, not generic "video" mentions
            if any(kw in val_lower for kw in VIDEO_PLAYER_KEYWORDS):
                text_signals["has_video_player"] = True
            # Post-answer feedback detection (buttons/links like "Next question")
            if any(kw in val_lower for kw in POST_ANSWER_KEYWORDS):
                text_signals["has_post_answer"] = True

        children = node.get("children")
        if children:
            stack.extend(children)

    return role_counts, text_signals
