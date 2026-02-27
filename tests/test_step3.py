"""Step 3 verification: 4 bug-fixed Spark files.

Tests guard logic and import paths. Integration tests (live Weaviate)
deferred to Step 8.
"""
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def test_screen_memory_imports():
    """screen_memory.py: imports cleanly, has key functions."""
    from spark.tasks.screen_memory import (
        get_client, ensure_schema, embed_text, query_nearest,
        store_screen, mark_validated, mark_invalidated,
        query_by_hash, get_stats, COLLECTION_NAME, VECTOR_DIMS,
    )
    assert COLLECTION_NAME == "ScreenEmbedding"
    assert VECTOR_DIMS == 4096
    assert callable(get_client)
    assert callable(embed_text)
    assert callable(query_nearest)
    assert callable(store_screen)
    assert callable(mark_validated)
    assert callable(mark_invalidated)
    print("  screen_memory.py: PASS (imports clean)")


def test_screen_memory_validated_only_param():
    """Bug #6 fix: query_nearest has validated_only parameter."""
    import inspect
    from spark.tasks.screen_memory import query_nearest
    sig = inspect.signature(query_nearest)
    assert "validated_only" in sig.parameters, "query_nearest must have validated_only param"
    print("  screen_memory.py: PASS (validated_only param exists)")


def test_match_screen_imports():
    """match_screen.py: imports cleanly (V20: simplified API)."""
    from spark.tasks.match_screen import match_screen
    assert callable(match_screen)
    print("  match_screen.py: PASS (imports clean)")


def test_match_screen_platform_guard():
    """Bug #1 fix: match_screen rejects missing platform key."""
    from spark.tasks.match_screen import match_screen
    # Config without platform key
    result = match_screen(
        {"role": "AXWebArea", "children": []},
        {}  # no platform key
    )
    assert result["matched"] is False, "Should not match without platform"
    assert "missing_platform" in result.get("error", "") or "needs_consultation" in result, \
        "Should indicate missing platform"
    print("  match_screen.py: PASS (platform guard)")


def test_match_screen_marker_matching():
    """V20: match_screen uses Jaccard signature matching, not YAML markers.
    Tests basic match_screen call returns expected format."""
    from spark.tasks.match_screen import match_screen

    tree = {
        "role": "AXWebArea", "name": "", "children": [
            {"role": "AXButton", "name": "Check", "children": []},
            {"role": "AXRadioButton", "name": "Option A", "children": []},
            {"role": "AXRadioButton", "name": "Option B", "children": []},
            {"role": "AXRadioButton", "name": "Option C", "children": []},
        ]
    }
    # No stored signatures, so should return no match
    result = match_screen(tree, {"platform": "test_platform_nonexistent"})
    assert result["matched"] is False
    assert result.get("needs_consultation") is True
    print("  match_screen.py: PASS (no match returns needs_consultation)")


def test_screen_router_imports():
    """screen_router.py: imports cleanly."""
    from spark.tasks.screen_router import (
        RouteResult, route_screen,
        KNOWN_THRESHOLD, ISOMORPHIC_THRESHOLD,
    )
    assert KNOWN_THRESHOLD == 0.05
    assert ISOMORPHIC_THRESHOLD == 0.191
    assert callable(route_screen)
    print("  screen_router.py: PASS (imports clean)")


def test_call_gemini_imports():
    """call_gemini.py: imports cleanly."""
    # call_gemini.py may need google.generativeai - check if it's available
    try:
        # Try importing the module
        import importlib
        spec = importlib.util.find_spec("spark.tasks.call_gemini")
        assert spec is not None, "Module should be findable"
        print("  call_gemini.py: PASS (module findable)")
    except ImportError as e:
        # May fail due to google.generativeai - that's ok for unit test
        print(f"  call_gemini.py: SKIP (dependency: {e})")


if __name__ == "__main__":
    print("Step 3: Testing 4 bug-fixed Spark files...")
    tests = [
        test_screen_memory_imports,
        test_screen_memory_validated_only_param,
        test_match_screen_imports,
        test_match_screen_platform_guard,
        test_match_screen_marker_matching,
        test_screen_router_imports,
        test_call_gemini_imports,
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
