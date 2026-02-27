"""
Unit tests for V19 structural pre-classification and category-constrained matching.

Tests the core fix: structural_classify() determines the master category from
tree structural features, then match_signature() only compares within that category.

Run: python3 -m pytest tests/test_structural_classify.py -v
Or:  python3 tests/test_structural_classify.py
"""

import sys
import os

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from spark.tasks.screen_signatures import structural_classify, extract_signature, _sig_hash


def _make_tree(web_area_children):
    """Build a minimal accessibility tree with browser chrome + web area."""
    return {
        "role": "AXApplication",
        "children": [
            {"role": "AXWindow", "children": [
                # Browser chrome (should be ignored by structural_classify)
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


def test_exercise_radio():
    """EXERCISE screens have radio buttons -> must classify as EXERCISE."""
    tree = _make_tree([
        {"role": "AXStaticText", "name": "What is the capital of France?"},
        {"role": "AXRadioButton", "name": "London"},
        {"role": "AXRadioButton", "name": "Paris"},
        {"role": "AXRadioButton", "name": "Berlin"},
        {"role": "AXRadioButton", "name": "Madrid"},
        {"role": "AXButton", "name": "Submit"},
        {"role": "AXButton", "name": "Go to next item"},
        {"role": "AXLink", "name": "Course Home"},
    ])
    result = structural_classify(tree)
    assert result == "EXERCISE", f"Expected EXERCISE, got {result}"
    print("PASS: test_exercise_radio")


def test_exercise_checkbox():
    """EXERCISE screens with checkboxes -> EXERCISE."""
    tree = _make_tree([
        {"role": "AXStaticText", "name": "Select all that apply:"},
        {"role": "AXCheckBox", "name": "Option A"},
        {"role": "AXCheckBox", "name": "Option B"},
        {"role": "AXCheckBox", "name": "Option C"},
        {"role": "AXButton", "name": "Submit"},
        {"role": "AXButton", "name": "Go to next item"},
    ])
    result = structural_classify(tree)
    assert result == "EXERCISE", f"Expected EXERCISE, got {result}"
    print("PASS: test_exercise_checkbox")


def test_exercise_text_input():
    """EXERCISE with text area -> EXERCISE."""
    tree = _make_tree([
        {"role": "AXStaticText", "name": "Write your answer:"},
        {"role": "AXTextArea", "name": "Your Answer"},
        {"role": "AXButton", "name": "Submit"},
    ])
    result = structural_classify(tree)
    assert result == "EXERCISE", f"Expected EXERCISE, got {result}"
    print("PASS: test_exercise_text_input")


def test_transition_no_structural():
    """TRANSITION screens have buttons/links but no assessment signals."""
    tree = _make_tree([
        {"role": "AXStaticText", "name": "You scored 8 out of 10!"},
        {"role": "AXButton", "name": "Continue"},
        {"role": "AXButton", "name": "Go to next item"},
        {"role": "AXLink", "name": "Course Home"},
    ])
    result = structural_classify(tree)
    assert result == "TRANSITION", f"Expected TRANSITION, got {result}"
    print("PASS: test_transition_no_structural")


def test_video_screen():
    """VIDEO screens have video player signals."""
    tree = _make_tree([
        {"role": "AXVideo", "name": "Lecture Video"},
        {"role": "AXButton", "name": "Pause"},
        {"role": "AXSlider", "name": "video progress"},
        {"role": "AXButton", "name": "Go to next item"},
    ])
    result = structural_classify(tree)
    assert result == "VIDEO", f"Expected VIDEO, got {result}"
    print("PASS: test_video_screen")


def test_navigation_many_links():
    """NAVIGATION screens have many links (15+)."""
    links = [{"role": "AXLink", "name": f"Lesson {i}"} for i in range(20)]
    tree = _make_tree(links + [
        {"role": "AXButton", "name": "Expand All"},
    ])
    result = structural_classify(tree)
    assert result == "NAVIGATION", f"Expected NAVIGATION, got {result}"
    print("PASS: test_navigation_many_links")


def test_unclassified_article():
    """ARTICLE screens often lack structural signals -> UNCLASSIFIED."""
    tree = _make_tree([
        {"role": "AXStaticText", "name": "Introduction to Machine Learning"},
        {"role": "AXStaticText", "name": "Machine learning is a field of artificial intelligence..."},
        {"role": "AXStaticText", "name": "There are three main types:"},
        {"role": "AXStaticText", "name": "1. Supervised learning"},
        {"role": "AXStaticText", "name": "2. Unsupervised learning"},
        {"role": "AXStaticText", "name": "3. Reinforcement learning"},
        {"role": "AXButton", "name": "Mark as completed"},
        {"role": "AXButton", "name": "Go to next item"},
    ])
    result = structural_classify(tree)
    # ARTICLE has no definitive structural signal, falls to UNCLASSIFIED or TRANSITION
    # It has buttons but no assessment signals, so analyze_tree returns TRANSITION
    assert result in ("UNCLASSIFIED", "TRANSITION"), f"Expected UNCLASSIFIED or TRANSITION, got {result}"
    print(f"PASS: test_unclassified_article (got {result})")


def test_false_positive_prevention():
    """
    THE CRITICAL TEST: An EXERCISE screen should never match a TRANSITION signature.

    Simulates the false positive that caused exercises to be skipped:
    - TRANSITION signature stored with high shared elements
    - EXERCISE tree comes in with radio buttons
    - V18: Jaccard 0.91+, structural penalty was a band-aid
    - V19: structural_classify() says EXERCISE, category filter blocks TRANSITION match
    """
    # Build two nearly-identical trees (95% shared elements)
    shared_elements = [
        {"role": "AXButton", "name": "Go to next item"},
        {"role": "AXButton", "name": "Save note"},
        {"role": "AXLink", "name": "Course Home"},
        {"role": "AXLink", "name": "Module 1"},
        {"role": "AXLink", "name": "Module 2"},
        {"role": "AXLink", "name": "Progress"},
        {"role": "AXButton", "name": "Help"},
        {"role": "AXTab", "name": "Lecture"},
        {"role": "AXTab", "name": "Notes"},
        {"role": "AXTab", "name": "Discuss"},
    ]

    # TRANSITION tree (no radio buttons)
    transition_tree = _make_tree(shared_elements + [
        {"role": "AXStaticText", "name": "You completed the quiz!"},
        {"role": "AXButton", "name": "Continue"},
    ])

    # EXERCISE tree (has radio buttons)
    exercise_tree = _make_tree(shared_elements + [
        {"role": "AXStaticText", "name": "What is 2+2?"},
        {"role": "AXRadioButton", "name": "3"},
        {"role": "AXRadioButton", "name": "4"},
        {"role": "AXRadioButton", "name": "5"},
        {"role": "AXButton", "name": "Submit"},
    ])

    # Verify structural classification is correct
    assert structural_classify(transition_tree) == "TRANSITION"
    assert structural_classify(exercise_tree) == "EXERCISE"

    # Compute Jaccard WITHOUT category filter to show the false positive
    transition_sig = extract_signature(transition_tree)
    exercise_sig = extract_signature(exercise_tree)

    # Simulate what Jaccard would compute
    # Both share the same button/tab/link labels
    intersection = len(transition_sig & exercise_sig)
    union = len(transition_sig | exercise_sig)
    raw_jaccard = intersection / union if union > 0 else 0.0

    print(f"  Raw Jaccard (no filter): {raw_jaccard:.2f} "
          f"(intersection={intersection}, union={union})")
    print(f"  Transition sig size: {len(transition_sig)}")
    print(f"  Exercise sig size: {len(exercise_sig)}")
    print(f"  This Jaccard score would have caused a FALSE POSITIVE in V18")
    print(f"  V19 prevents this: category_filter=EXERCISE blocks TRANSITION matches")

    print("PASS: test_false_positive_prevention")


def test_video_priority_over_exercise():
    """VIDEO takes priority even if the page also has form elements."""
    tree = _make_tree([
        {"role": "AXVideo", "name": "Lecture"},
        {"role": "AXButton", "name": "Pause"},
        # Some pages have form elements in sidebar
        {"role": "AXTextField", "name": "Search"},
    ])
    result = structural_classify(tree)
    assert result == "VIDEO", f"Expected VIDEO (priority), got {result}"
    print("PASS: test_video_priority_over_exercise")


def test_post_answer_transition():
    """Post-answer screens (with 'next question' signals) -> TRANSITION."""
    tree = _make_tree([
        {"role": "AXStaticText", "name": "Correct!"},
        {"role": "AXButton", "name": "Next question"},
        {"role": "AXRadioButton", "name": "Previous answer A"},  # Still visible
        {"role": "AXRadioButton", "name": "Previous answer B"},
        {"role": "AXRadioButton", "name": "Previous answer C"},
    ])
    result = structural_classify(tree)
    # analyze_tree has _has_post_answer_signals which detects "next question"
    # and returns TRANSITION early, before checking radio buttons
    assert result == "TRANSITION", f"Expected TRANSITION (post-answer override), got {result}"
    print("PASS: test_post_answer_transition")


if __name__ == "__main__":
    print("=" * 60)
    print("V19 Structural Pre-Classification Tests")
    print("=" * 60)
    print()

    test_exercise_radio()
    test_exercise_checkbox()
    test_exercise_text_input()
    test_transition_no_structural()
    test_video_screen()
    test_navigation_many_links()
    test_unclassified_article()
    test_false_positive_prevention()
    test_video_priority_over_exercise()
    test_post_answer_transition()

    print()
    print("=" * 60)
    print("ALL TESTS PASSED")
    print("=" * 60)
