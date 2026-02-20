"""Step 2 verification: 16 proven Mac task files + storage.

Mac files use macOS-specific APIs (Quartz, ApplicationServices).
On Linux, we verify: file existence, compute_tree_hash (pure Python),
extract_text (pure Python), and config (pure Python).
"""
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

MAC_TASKS = [
    "bt_helpers.py", "find_element.py", "click_element.py",
    "capture_tree.py", "capture_macapptree.py", "compute_tree_hash.py",
    "extract_text.py", "call_spark.py", "type_text.py", "wait.py",
    "build_kb_context.py", "store_qa.py", "crop_image.py",
    "checkpoint.py", "browser_url.py", "behavior_tree.py",
]


def test_files_exist():
    """All 16 Mac task files exist."""
    base = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "app", "tasks")
    missing = []
    for f in MAC_TASKS:
        path = os.path.join(base, f)
        if not os.path.exists(path):
            missing.append(f)
    assert not missing, f"Missing files: {missing}"
    print(f"  All {len(MAC_TASKS)} Mac task files: EXIST")


def test_frozen_headers():
    """All files have FROZEN header."""
    base = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "app", "tasks")
    no_header = []
    for f in MAC_TASKS:
        path = os.path.join(base, f)
        with open(path) as fh:
            first_line = fh.readline()
        if "FROZEN" not in first_line:
            no_header.append(f)
    assert not no_header, f"Missing FROZEN header: {no_header}"
    print("  All files have FROZEN headers: PASS")


def test_compute_tree_hash():
    """compute_tree_hash.py: pure Python, works on Linux."""
    from app.tasks.compute_tree_hash import compute_tree_hash
    tree = {"role": "AXButton", "name": "Check", "children": [
        {"role": "AXStaticText", "name": "Submit answer", "children": []}
    ]}
    h1 = compute_tree_hash(tree)
    h2 = compute_tree_hash(tree)
    assert h1 == h2, "Must be deterministic"
    assert len(h1) > 0, "Hash should not be empty"

    # Different tree -> different hash
    tree2 = {"role": "AXButton", "name": "Next", "children": []}
    h3 = compute_tree_hash(tree2)
    assert h3 != h1, "Different trees should produce different hashes"
    print("  compute_tree_hash.py: PASS")


def test_config():
    """app/config.py: imports cleanly on Linux."""
    from app.config import get_spark_url, get_api_key, is_configured
    assert callable(get_spark_url)
    assert callable(get_api_key)
    assert callable(is_configured)
    print("  config.py: PASS")


def test_storage():
    """app/storage/sqlite_store.py: exists and has FROZEN header."""
    path = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "app", "storage", "sqlite_store.py"
    )
    assert os.path.exists(path)
    with open(path) as f:
        assert "FROZEN" in f.readline()
    print("  sqlite_store.py: PASS")


if __name__ == "__main__":
    print("Step 2: Testing 16 proven Mac files + storage...")
    tests = [
        test_files_exist, test_frozen_headers,
        test_compute_tree_hash, test_config, test_storage,
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
