"""
Unit tests for V20: threshold-free presence detection and unconstrained matching.

V20 changes tested:
- analyze_tree() uses presence-only checks (no count thresholds)
- True/false questions (2 radio buttons) correctly get HAS_RADIO tag
- No structural_classify() — classification is always by Gemini for unmatched screens
- match_signature() runs against ALL signatures (no category filter)

Run: python3 -m pytest tests/test_v20_no_thresholds.py -v
Or:  python3 tests/test_v20_no_thresholds.py
"""

import sys
import os

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from spark.tasks.prompt_codex import analyze_tree
from spark.tasks.screen_signatures import extract_signature


def _make_tree(web_area_children):
    """Build a minimal accessibility tree with browser chrome + web area."""
    return {
        "role": "AXApplication",
        "children": [
            {"role": "AXWindow", "children": [
                # Browser chrome (should be ignored by analyze_tree)
                {"role": "AXToolbar", "children": [
                    {"role": "AXButton", "name": "Back"},
                    {"role": "AXButton", "name": "Forward"},
                    {"role": "AXButton", "name": "Reload"},
                    {"role": "AXTextField", "name": "Address"},
                ]},
                # Web content
                {"role": "AXWebArea", "children": web_area_children},
            ]},
        ],
    }


# ── Core Fix: True/False Questions ──

def test_true_false_two_radio_buttons():
    """THE CRITICAL FIX: 2 radio buttons MUST get HAS_RADIO tag.

    This was the root cause Jesse identified. Old code required >= 3 radio
    buttons, so true/false questions were misclassified as TRANSITION.
    """
    tree = _make_tree([
        {"role": "AXStaticText", "name": "Is the Earth flat?"},
        {"role": "AXRadioButton", "name": "True"},
        {"role": "AXRadioButton", "name": "False"},
        {"role": "AXButton", "name": "Submit"},
    ])
    tags = analyze_tree(tree)
    assert "HAS_RADIO" in tags, f"2 radio buttons MUST produce HAS_RADIO. Got: {tags}"
    print("PASS: test_true_false_two_radio_buttons")


def test_single_checkbox():
    """Single checkbox (e.g., 'I agree' or 'Select if true') gets HAS_CHECKBOX."""
    tree = _make_tree([
        {"role": "AXStaticText", "name": "Do you agree to the terms?"},
        {"role": "AXCheckBox", "name": "I agree"},
        {"role": "AXButton", "name": "Continue"},
    ])
    tags = analyze_tree(tree)
    assert "HAS_CHECKBOX" in tags, f"1 checkbox MUST produce HAS_CHECKBOX. Got: {tags}"
    print("PASS: test_single_checkbox")


def test_single_dropdown():
    """Single dropdown exercise gets HAS_COMBOBOX."""
    tree = _make_tree([
        {"role": "AXStaticText", "name": "Select the correct answer:"},
        {"role": "AXComboBox", "name": "Choose one"},
        {"role": "AXButton", "name": "Check"},
    ])
    tags = analyze_tree(tree)
    assert "HAS_COMBOBOX" in tags, f"1 combobox MUST produce HAS_COMBOBOX. Got: {tags}"
    print("PASS: test_single_dropdown")


# ── Presence Detection (No Thresholds) ──

def test_radio_buttons_any_count():
    """Any number of radio buttons produces HAS_RADIO."""
    for count in [1, 2, 3, 5, 10]:
        radios = [{"role": "AXRadioButton", "name": f"Option {i}"} for i in range(count)]
        tree = _make_tree(radios + [{"role": "AXButton", "name": "Submit"}])
        tags = analyze_tree(tree)
        assert "HAS_RADIO" in tags, f"{count} radios should produce HAS_RADIO. Got: {tags}"
    print("PASS: test_radio_buttons_any_count")


def test_links_any_count():
    """Any number of links produces HAS_LINKS (not HAS_MANY_LINKS)."""
    for count in [1, 5, 10, 20]:
        links = [{"role": "AXLink", "name": f"Item {i}"} for i in range(count)]
        tree = _make_tree(links)
        tags = analyze_tree(tree)
        assert "HAS_LINKS" in tags, f"{count} links should produce HAS_LINKS. Got: {tags}"
        assert "HAS_MANY_LINKS" not in tags, "HAS_MANY_LINKS tag should not exist in V20"
    print("PASS: test_links_any_count")


def test_no_transition_tag():
    """V20: TRANSITION is never produced as a tag. Gemini classifies transitions."""
    tree = _make_tree([
        {"role": "AXStaticText", "name": "You completed the quiz!"},
        {"role": "AXButton", "name": "Continue"},
    ])
    tags = analyze_tree(tree)
    assert "TRANSITION" not in tags, f"TRANSITION tag should not exist in V20. Got: {tags}"
    assert "HAS_BUTTONS" in tags, f"Buttons should be detected. Got: {tags}"
    print("PASS: test_no_transition_tag")


def test_no_post_answer_short_circuit():
    """V20: 'next question' text should NOT short-circuit to TRANSITION.

    Old code had _has_post_answer_signals() which would force TRANSITION
    classification if text like 'next question' appeared anywhere, even on
    a page that was actually an exercise with radio buttons.
    """
    tree = _make_tree([
        {"role": "AXStaticText", "name": "Correct! Click next question to continue."},
        {"role": "AXRadioButton", "name": "Previous answer A"},
        {"role": "AXRadioButton", "name": "Previous answer B"},
        {"role": "AXButton", "name": "Next question"},
    ])
    tags = analyze_tree(tree)
    # Should have BOTH radio and button tags — no short-circuit
    assert "HAS_RADIO" in tags, f"Radio buttons must be detected. Got: {tags}"
    assert "TRANSITION" not in tags, f"TRANSITION tag should not appear. Got: {tags}"
    print("PASS: test_no_post_answer_short_circuit")


# ── Video Detection (Still Works) ──

def test_video_detection_by_role():
    """Video player detection via AXVideo role still works."""
    tree = _make_tree([
        {"role": "AXVideo", "name": "Lecture Video"},
        {"role": "AXButton", "name": "Pause"},
    ])
    tags = analyze_tree(tree)
    assert "HAS_VIDEO" in tags, f"AXVideo must produce HAS_VIDEO. Got: {tags}"
    print("PASS: test_video_detection_by_role")


def test_video_detection_by_keyword():
    """Video player detection via keywords still works."""
    tree = _make_tree([
        {"role": "AXGroup", "name": "video player container"},
        {"role": "AXButton", "name": "Play"},
    ])
    tags = analyze_tree(tree)
    assert "HAS_VIDEO" in tags, f"Video keywords must produce HAS_VIDEO. Got: {tags}"
    print("PASS: test_video_detection_by_keyword")


# ── Multiple Tags Coexist ──

def test_multiple_tags():
    """A complex screen should report ALL present element types."""
    tree = _make_tree([
        {"role": "AXRadioButton", "name": "Option A"},
        {"role": "AXRadioButton", "name": "Option B"},
        {"role": "AXTextArea", "name": "Explanation"},
        {"role": "AXButton", "name": "Submit"},
        {"role": "AXLink", "name": "Help"},
    ])
    tags = analyze_tree(tree)
    assert "HAS_RADIO" in tags, f"Missing HAS_RADIO. Got: {tags}"
    assert "HAS_TEXT_INPUT" in tags, f"Missing HAS_TEXT_INPUT. Got: {tags}"
    assert "HAS_BUTTONS" in tags, f"Missing HAS_BUTTONS. Got: {tags}"
    assert "HAS_LINKS" in tags, f"Missing HAS_LINKS. Got: {tags}"
    print("PASS: test_multiple_tags")


# ── structural_classify Removed ──

def test_structural_classify_removed():
    """structural_classify() should no longer exist in screen_signatures."""
    import spark.tasks.screen_signatures as ss
    assert not hasattr(ss, "structural_classify"), \
        "structural_classify() should be removed in V20"
    print("PASS: test_structural_classify_removed")


# ── Signature Matching (No Category Filter) ──

def test_match_signature_no_category_param():
    """match_signature() should NOT accept category_filter parameter."""
    import inspect
    from spark.tasks.screen_signatures import match_signature
    sig = inspect.signature(match_signature)
    params = list(sig.parameters.keys())
    assert "category_filter" not in params, \
        f"category_filter parameter should be removed. Params: {params}"
    print("PASS: test_match_signature_no_category_param")


if __name__ == "__main__":
    print("=" * 60)
    print("V20 No-Threshold Tests")
    print("=" * 60)
    print()

    test_true_false_two_radio_buttons()
    test_single_checkbox()
    test_single_dropdown()
    test_radio_buttons_any_count()
    test_links_any_count()
    test_no_transition_tag()
    test_no_post_answer_short_circuit()
    test_video_detection_by_role()
    test_video_detection_by_keyword()
    test_multiple_tags()
    test_structural_classify_removed()
    test_match_signature_no_category_param()

    print()
    print("=" * 60)
    print("ALL TESTS PASSED")
    print("=" * 60)
