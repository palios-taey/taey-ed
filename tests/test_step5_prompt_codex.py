"""Step 5 verification: prompt_codex.py — THE heart of V8.

Tests:
  1. analyze_tree returns HAS_RADIO for 3+ radio buttons
  2. analyze_tree returns HAS_VIDEO for video signals
  3. analyze_tree returns HAS_LINKS for any links (V20: no count threshold)
  4. analyze_tree returns HAS_CHECKBOX for 3+ checkboxes
  5. analyze_tree returns HAS_TEXT_INPUT for text areas
  6. analyze_tree returns HAS_COMBOBOX for combo boxes
  7. V20: TRANSITION tag no longer exists. Buttons produce HAS_BUTTONS.
  8. compile_prompt output is 20K-60K chars
  9. Output includes all 16 handler names
  10. Output includes detected pattern (RADIO for radio tree)
  11. Output includes platform knowledge section
  12. Output includes cardinal rules (NEVER, fallback)
  13. Reconsultation context appears when spark_attempts > 0
  14. Multiple signals detected (radio + text input)
"""
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from spark.tasks.prompt_codex import (
    analyze_tree,
    compile_prompt,
    SCREEN_PATTERNS,
    SECTION_5_HANDLERS,
)


# =========================================================================
# Mock Trees
# =========================================================================

def _make_tree(roles: dict, web_area: bool = True) -> dict:
    """Build a mock tree with specified role counts."""
    children = []
    for role, count in roles.items():
        for i in range(count):
            children.append({
                "role": role,
                "name": f"{role}_{i}",
                "title": f"Option {i}" if "Radio" in role or "Check" in role else f"Item {i}",
            })

    if web_area:
        return {
            "role": "AXApplication",
            "children": [{
                "role": "AXWebArea",
                "children": children,
            }]
        }
    return {"role": "AXWindow", "children": children}


RADIO_TREE = _make_tree({"AXRadioButton": 4, "AXButton": 2, "AXStaticText": 3})
CHECKBOX_TREE = _make_tree({"AXCheckBox": 5, "AXButton": 1})
TEXT_INPUT_TREE = _make_tree({"AXTextArea": 1, "AXButton": 2})
MANY_LINKS_TREE = _make_tree({"AXLink": 20, "AXButton": 1})
COMBO_TREE = _make_tree({"AXComboBox": 3, "AXButton": 1})
BUTTON_ONLY_TREE = _make_tree({"AXButton": 3})
RADIO_WITH_TEXT_TREE = _make_tree({"AXRadioButton": 4, "AXTextArea": 1, "AXButton": 2})

VIDEO_TREE = {
    "role": "AXApplication",
    "children": [{
        "role": "AXWebArea",
        "children": [
            {"role": "AXVideo", "name": "Lesson video"},
            {"role": "AXButton", "name": "Play"},
        ]
    }]
}

VIDEO_KEYWORD_TREE = {
    "role": "AXApplication",
    "children": [{
        "role": "AXWebArea",
        "children": [
            {"role": "AXGroup", "name": "video player controls"},
            {"role": "AXButton", "name": "Pause"},
        ]
    }]
}


# =========================================================================
# Tests
# =========================================================================

def test_analyze_radio():
    """1. analyze_tree returns HAS_RADIO for 3+ radio buttons."""
    tags = analyze_tree(RADIO_TREE)
    assert "HAS_RADIO" in tags, f"Expected HAS_RADIO, got {tags}"
    print("  1. HAS_RADIO detected: PASS")


def test_analyze_video():
    """2. analyze_tree returns HAS_VIDEO for video signals."""
    tags = analyze_tree(VIDEO_TREE)
    assert "HAS_VIDEO" in tags, f"Expected HAS_VIDEO, got {tags}"
    # Also test keyword-based detection
    tags2 = analyze_tree(VIDEO_KEYWORD_TREE)
    assert "HAS_VIDEO" in tags2, f"Expected HAS_VIDEO from keywords, got {tags2}"
    print("  2. HAS_VIDEO detected (role + keyword): PASS")


def test_analyze_many_links():
    """3. analyze_tree returns HAS_LINKS for any links (V20: no count threshold)."""
    tags = analyze_tree(MANY_LINKS_TREE)
    assert "HAS_LINKS" in tags, f"Expected HAS_LINKS, got {tags}"
    print("  3. HAS_LINKS detected: PASS")


def test_analyze_checkbox():
    """4. analyze_tree returns HAS_CHECKBOX for 3+ checkboxes."""
    tags = analyze_tree(CHECKBOX_TREE)
    assert "HAS_CHECKBOX" in tags, f"Expected HAS_CHECKBOX, got {tags}"
    print("  4. HAS_CHECKBOX detected: PASS")


def test_analyze_text_input():
    """5. analyze_tree returns HAS_TEXT_INPUT for text areas."""
    tags = analyze_tree(TEXT_INPUT_TREE)
    assert "HAS_TEXT_INPUT" in tags, f"Expected HAS_TEXT_INPUT, got {tags}"
    print("  5. HAS_TEXT_INPUT detected: PASS")


def test_analyze_combobox():
    """6. analyze_tree returns HAS_COMBOBOX for combo boxes."""
    tags = analyze_tree(COMBO_TREE)
    assert "HAS_COMBOBOX" in tags, f"Expected HAS_COMBOBOX, got {tags}"
    print("  6. HAS_COMBOBOX detected: PASS")


def test_analyze_transition():
    """7. V20: TRANSITION tag no longer exists. Buttons produce HAS_BUTTONS."""
    tags = analyze_tree(BUTTON_ONLY_TREE)
    assert "HAS_BUTTONS" in tags, f"Expected HAS_BUTTONS, got {tags}"
    assert "TRANSITION" not in tags, f"TRANSITION tag should not exist in V20. Got: {tags}"
    print("  7. HAS_BUTTONS detected (no TRANSITION): PASS")


def test_compile_prompt_size():
    """8. compile_prompt output is 20K-60K chars."""
    prompt = compile_prompt(
        tree=RADIO_TREE,
        platform="khan_academy",
        consultation_id="test-123",
        context={"escalation_level": "spark_claude", "course_id": "test_course"},
        spark_attempts=0,
    )
    length = len(prompt)
    assert length > 20000, f"Prompt too short: {length} chars (need >20K)"
    assert length < 60000, f"Prompt too long: {length} chars (need <60K)"
    print(f"  8. Prompt size: {length} chars: PASS")


def test_compile_prompt_handlers():
    """9. Output includes all 16 handler names."""
    prompt = compile_prompt(
        tree=RADIO_TREE,
        platform="khan_academy",
        consultation_id="test-123",
        context={},
    )
    handlers = [
        "find_and_click", "find_and_type", "find_all", "click",
        "extract_question", "send_to_llm", "video_poll", "wait",
        "press_key", "scroll", "wait_for_element", "discover_menu",
        "lookup_match", "store_qa", "solve_assessment_page", "press_escape",
    ]
    missing = [h for h in handlers if h not in prompt]
    assert not missing, f"Missing handlers in prompt: {missing}"
    print("  9. All 16 handlers present: PASS")


def test_compile_prompt_pattern():
    """10. Output includes detected pattern (RADIO for radio tree)."""
    prompt = compile_prompt(
        tree=RADIO_TREE,
        platform="khan_academy",
        consultation_id="test-123",
        context={},
    )
    assert "RADIO BUTTONS" in prompt, "Missing RADIO BUTTONS pattern section"
    assert "extract_question" in prompt, "Missing extract_question in pattern"
    print("  10. Detected pattern included: PASS")


def test_compile_prompt_knowledge():
    """11. Output includes platform knowledge section."""
    prompt = compile_prompt(
        tree=RADIO_TREE,
        platform="khan_academy",
        consultation_id="test-123",
        context={},
    )
    assert "PLATFORM KNOWLEDGE" in prompt, "Missing platform knowledge section"
    print("  11. Platform knowledge section included: PASS")


def test_compile_prompt_cardinal_rules():
    """12. Output includes cardinal rules (NEVER, fallback)."""
    prompt = compile_prompt(
        tree=RADIO_TREE,
        platform="khan_academy",
        consultation_id="test-123",
        context={},
    )
    assert "CARDINAL RULES" in prompt, "Missing CARDINAL RULES"
    assert "FALLBACK NODES ARE BANNED" in prompt, "Missing fallback ban"
    assert "NEVER" in prompt, "Missing NEVER rules"
    print("  12. Cardinal rules present: PASS")


def test_reconsultation_context():
    """13. Reconsultation context appears when spark_attempts > 0."""
    prompt = compile_prompt(
        tree=RADIO_TREE,
        platform="khan_academy",
        consultation_id="test-123",
        context={"failure_reason": "wrong_answer", "previous_screen_type": "EXERCISE_RADIO"},
        spark_attempts=1,
        is_reconsultation=True,
    )
    assert "RECONSULTATION WARNING" in prompt, "Missing reconsultation section"
    assert "attempt #2" in prompt, "Missing attempt number"
    assert "wrong_answer" in prompt, "Missing failure reason"
    assert "bt_debug.log" in prompt, "Missing bt_debug.log reference"
    print("  13. Reconsultation context: PASS")


def test_multiple_signals():
    """14. Multiple signals detected (radio + text input)."""
    tags = analyze_tree(RADIO_WITH_TEXT_TREE)
    assert "HAS_RADIO" in tags, f"Expected HAS_RADIO in {tags}"
    assert "HAS_TEXT_INPUT" in tags, f"Expected HAS_TEXT_INPUT in {tags}"
    print(f"  14. Multiple signals {tags}: PASS")


if __name__ == "__main__":
    print("Step 5: Testing prompt_codex.py...")
    tests = [
        test_analyze_radio,
        test_analyze_video,
        test_analyze_many_links,
        test_analyze_checkbox,
        test_analyze_text_input,
        test_analyze_combobox,
        test_analyze_transition,
        test_compile_prompt_size,
        test_compile_prompt_handlers,
        test_compile_prompt_pattern,
        test_compile_prompt_knowledge,
        test_compile_prompt_cardinal_rules,
        test_reconsultation_context,
        test_multiple_signals,
    ]
    failures = []
    for t in tests:
        try:
            t()
        except Exception as e:
            failures.append((t.__name__, str(e)))
            print(f"  {t.__name__}: FAIL - {e}")

    print(f"\nResults: {len(tests) - len(failures)}/{len(tests)} passed")
    if failures:
        for name, err in failures:
            print(f"  FAIL: {name}: {err}")
        sys.exit(1)
    else:
        print("ALL PASS")
