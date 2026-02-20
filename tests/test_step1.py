"""Step 1 verification: 11 proven Spark task files.

Tests what can be tested NOW (Steps 1 only).
Files with cross-deps on Step 3 (screen_memory) are import-tested later.
"""
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def test_skeleton():
    """skeleton.py: extract + hash on synthetic tree."""
    from spark.tasks.skeleton import extract_skeleton, skeleton_hash, extract_dynamic_text

    tree = {
        "role": "AXWebArea", "name": "", "position": [0, 0], "size": [1200, 900],
        "children": [
            {"role": "AXGroup", "name": "", "position": [0, 0], "size": [1200, 400], "children": [
                {"role": "AXStaticText", "name": "What is 2+2?", "position": [100, 100], "size": [400, 30], "children": []},
                {"role": "AXRadioButton", "name": "3", "position": [100, 200], "size": [400, 30], "children": []},
                {"role": "AXRadioButton", "name": "4", "position": [100, 250], "size": [400, 30], "children": []},
                {"role": "AXRadioButton", "name": "5", "position": [100, 300], "size": [400, 30], "children": []},
            ]},
            {"role": "AXButton", "name": "Check", "position": [600, 400], "size": [100, 40], "children": []},
        ]
    }
    skel = extract_skeleton(tree)
    assert len(skel) > 0, "Skeleton should not be empty"
    h = skeleton_hash(skel)
    assert len(h) == 16, f"Hash should be 16 chars, got {len(h)}"
    assert skeleton_hash(extract_skeleton(tree)) == h, "Deterministic"
    texts = extract_dynamic_text(tree)
    assert isinstance(texts, list)
    print("  skeleton.py: PASS")


def test_atomic_write():
    """atomic_write.py: imports and is callable."""
    from spark.tasks.atomic_write import atomic_write_json
    assert callable(atomic_write_json)
    print("  atomic_write.py: PASS")


def test_load_yaml():
    """load_yaml.py: loads khan_academy config."""
    from spark.tasks.load_yaml import load_yaml
    config = load_yaml("khan_academy")
    assert config is not None
    assert config.get("platform") == "khan_academy"
    print("  load_yaml.py: PASS")


def test_validate_config():
    """validate_config.py: validate_config callable."""
    from spark.tasks.validate_config import validate_config
    assert callable(validate_config)
    # Test with empty config (should return errors list, not crash)
    errors = validate_config({})
    assert isinstance(errors, list)
    print("  validate_config.py: PASS")


def test_consultation_state():
    """consultation_state.py: imports."""
    from spark.tasks.consultation_state import get_consultation_state
    assert callable(get_consultation_state)
    print("  consultation_state.py: PASS")


def test_consultation_escalate():
    """consultation_escalate.py: imports."""
    from spark.tasks.consultation_escalate import escalate_consultation
    assert callable(escalate_consultation)
    print("  consultation_escalate.py: PASS")


def test_notify_tmux():
    """notify_tmux.py: imports."""
    from spark.tasks.notify_tmux import notify_spark_claude
    assert callable(notify_spark_claude)
    print("  notify_tmux.py: PASS")


def test_call_embedding():
    """call_embedding.py: get_embeddings is async callable."""
    from spark.tasks.call_embedding import get_embeddings
    assert callable(get_embeddings)
    print("  call_embedding.py: PASS")


def test_consultation_respond():
    """consultation_respond.py: respond_to_consultation callable."""
    from spark.tasks.consultation_respond import respond_to_consultation
    assert callable(respond_to_consultation)
    print("  consultation_respond.py: PASS")


def test_deferred_imports():
    """Files with Step 3 dependencies: verify file exists, defer full import."""
    import importlib
    # screen_collapse depends on screen_memory (Step 3) - just verify file exists
    assert os.path.exists(os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "spark", "tasks", "screen_collapse.py"
    )), "screen_collapse.py should exist"
    # call_vision depends on google.generativeai - verify file exists
    assert os.path.exists(os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "spark", "tasks", "call_vision.py"
    )), "call_vision.py should exist"
    print("  screen_collapse.py: EXISTS (import deferred to Step 3)")
    print("  call_vision.py: EXISTS (import deferred - needs google.generativeai)")


if __name__ == "__main__":
    print("Step 1: Testing 11 proven Spark files...")
    tests = [
        test_skeleton, test_atomic_write, test_load_yaml,
        test_validate_config, test_consultation_state,
        test_consultation_escalate, test_notify_tmux,
        test_call_embedding, test_consultation_respond,
        test_deferred_imports,
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
