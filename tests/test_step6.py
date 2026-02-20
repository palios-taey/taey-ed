"""Step 6 verification: consultation_request.py rewrite.

Tests:
  1. consultation_request.py imports cleanly
  2. handle_consultation.py re-exports all expected symbols
  3. request_consultation function signature matches expected
  4. check_consultation function signature matches expected
  5. get_pending_consultations function signature matches expected
  6. No references to build_consultation_prompt (replaced by prompt_codex)
  7. No references to RECIPES_DIR or WARNINGS_DIR (dead code removed)
  8. Uses prompt_codex.compile_prompt instead
  9. CONSULT_DIR constant exists
  10. 1-at-a-time enforcement logic present
"""
import sys
import os
import inspect

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def test_consultation_request_imports():
    """1. consultation_request.py imports cleanly."""
    from spark.tasks.consultation_request import (
        request_consultation,
        check_consultation,
        get_pending_consultations,
        CONSULT_DIR,
    )
    assert callable(request_consultation)
    assert callable(check_consultation)
    assert callable(get_pending_consultations)
    print("  1. consultation_request imports: PASS")


def test_handle_consultation_reexports():
    """2. handle_consultation.py re-exports all expected symbols."""
    from spark.tasks.handle_consultation import (
        ConsultationState,
        get_consultation_state,
        set_consultation_state,
        compute_tree_hash,
        request_consultation,
        check_consultation,
        get_pending_consultations,
        CONSULT_DIR,
        respond_to_consultation,
        escalate_consultation,
    )
    assert callable(request_consultation)
    assert callable(respond_to_consultation)
    assert callable(escalate_consultation)
    print("  2. handle_consultation re-exports: PASS")


def test_request_consultation_signature():
    """3. request_consultation function signature matches expected."""
    from spark.tasks.consultation_request import request_consultation
    sig = inspect.signature(request_consultation)
    params = list(sig.parameters.keys())
    assert "platform" in params, f"Missing 'platform' in {params}"
    assert "tree" in params, f"Missing 'tree' in {params}"
    assert "screenshot_b64" in params, f"Missing 'screenshot_b64' in {params}"
    assert "context" in params, f"Missing 'context' in {params}"
    assert "bt_debug_log" in params, f"Missing 'bt_debug_log' in {params}"
    print("  3. request_consultation signature: PASS")


def test_check_consultation_signature():
    """4. check_consultation function signature matches expected."""
    from spark.tasks.consultation_request import check_consultation
    sig = inspect.signature(check_consultation)
    params = list(sig.parameters.keys())
    assert "consultation_id" in params, f"Missing 'consultation_id' in {params}"
    print("  4. check_consultation signature: PASS")


def test_get_pending_consultations_signature():
    """5. get_pending_consultations function signature matches expected."""
    from spark.tasks.consultation_request import get_pending_consultations
    sig = inspect.signature(get_pending_consultations)
    params = list(sig.parameters.keys())
    assert len(params) == 0, f"Expected no params, got {params}"
    print("  5. get_pending_consultations signature: PASS")


def test_no_build_consultation_prompt():
    """6. No references to build_consultation_prompt (replaced by prompt_codex)."""
    path = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "spark", "tasks", "consultation_request.py"
    )
    with open(path) as f:
        content = f.read()
    assert "build_consultation_prompt" not in content, \
        "Still references build_consultation_prompt (should use prompt_codex)"
    print("  6. No build_consultation_prompt refs: PASS")


def test_no_dead_directories():
    """7. No references to RECIPES_DIR or WARNINGS_DIR (dead code removed)."""
    path = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "spark", "tasks", "consultation_request.py"
    )
    with open(path) as f:
        content = f.read()
    assert "RECIPES_DIR" not in content, "Still references RECIPES_DIR"
    assert "WARNINGS_DIR" not in content, "Still references WARNINGS_DIR"
    print("  7. No dead directory refs: PASS")


def test_uses_prompt_codex():
    """8. Uses prompt_codex.compile_prompt instead."""
    path = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "spark", "tasks", "consultation_request.py"
    )
    with open(path) as f:
        content = f.read()
    assert "prompt_codex" in content, "Missing prompt_codex reference"
    assert "compile_prompt" in content, "Missing compile_prompt reference"
    print("  8. Uses prompt_codex: PASS")


def test_consult_dir():
    """9. CONSULT_DIR constant exists."""
    from spark.tasks.consultation_request import CONSULT_DIR
    assert str(CONSULT_DIR) == "/tmp/taey-ed-consult", \
        f"CONSULT_DIR wrong: {CONSULT_DIR}"
    print("  9. CONSULT_DIR: PASS")


def test_one_at_a_time():
    """10. 1-at-a-time enforcement logic present."""
    path = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "spark", "tasks", "consultation_request.py"
    )
    with open(path) as f:
        content = f.read()
    assert "ONE AT A TIME" in content or "one at a time" in content.lower(), \
        "Missing 1-at-a-time enforcement"
    assert '"existing"' in content, "Missing 'existing' status return"
    print("  10. 1-at-a-time enforcement: PASS")


if __name__ == "__main__":
    print("Step 6: Testing consultation_request.py rewrite...")
    tests = [
        test_consultation_request_imports,
        test_handle_consultation_reexports,
        test_request_consultation_signature,
        test_check_consultation_signature,
        test_get_pending_consultations_signature,
        test_no_build_consultation_prompt,
        test_no_dead_directories,
        test_uses_prompt_codex,
        test_consult_dir,
        test_one_at_a_time,
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
