"""Step 7 verification: server.py split into modular routes.

Tests:
  1. All 7 route modules import cleanly
  2. models.py has all Pydantic models
  3. server.py imports all routers
  4. Each router has expected endpoints
  5. No monolith — server.py is under 150 lines
  6. next_action.py contains bug fix comments
  7. validate_action.py and action_review.py exist
  8. Import paths use spark.tasks (not bare tasks.)
"""
import sys
import os
import importlib

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def test_route_modules_import():
    """1. All 7 route modules import cleanly."""
    modules = [
        "spark.routes.health",
        "spark.routes.next_action",
        "spark.routes.consultation",
        "spark.routes.compute",
        "spark.routes.review",
        "spark.routes.spinal_cord",
        "spark.routes.validation",
    ]
    for mod_name in modules:
        try:
            importlib.import_module(mod_name)
        except Exception as e:
            raise AssertionError(f"Failed to import {mod_name}: {e}")
    print(f"  1. All {len(modules)} route modules import: PASS")


def test_models_import():
    """2. models.py has all Pydantic models."""
    from spark.models import (
        MatchRequest, ConsultRequest, ConsultResponseRequest,
        EscalateRequest, ValidateRequest, ExtractImageRequest,
        EmbedRequest, GenerateRequest, ActionReviewRequest,
        ActionReviewResponseRequest, RouteRequest, CollapseRequest,
        ClientState, LastResult, NextActionRequest,
    )
    # Verify they're actual Pydantic models
    assert hasattr(NextActionRequest, "model_fields"), \
        "NextActionRequest is not a Pydantic model"
    print("  2. All 15 Pydantic models import: PASS")


def test_server_imports_routers():
    """3. server.py imports all routers."""
    path = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "spark", "server.py"
    )
    with open(path) as f:
        content = f.read()
    routers = [
        "health_router", "next_action_router", "consultation_router",
        "compute_router", "review_router", "spinal_cord_router",
        "validation_router",
    ]
    missing = [r for r in routers if r not in content]
    assert not missing, f"Missing routers in server.py: {missing}"
    print("  3. server.py imports all 7 routers: PASS")


def test_endpoints_exist():
    """4. Each router has expected endpoints."""
    from spark.routes.health import router as hr
    from spark.routes.next_action import router as nar
    from spark.routes.consultation import router as cr
    from spark.routes.compute import router as cor
    from spark.routes.spinal_cord import router as scr

    # Check route counts
    assert len(hr.routes) >= 2, f"health has {len(hr.routes)} routes, expected 2+"
    assert len(nar.routes) >= 1, f"next_action has {len(nar.routes)} routes, expected 1+"
    assert len(cr.routes) >= 4, f"consultation has {len(cr.routes)} routes, expected 4+"
    assert len(cor.routes) >= 3, f"compute has {len(cor.routes)} routes, expected 3+"
    assert len(scr.routes) >= 2, f"spinal_cord has {len(scr.routes)} routes, expected 2+"
    print("  4. All routers have expected endpoints: PASS")


def test_server_is_small():
    """5. No monolith — server.py is under 150 lines."""
    path = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "spark", "server.py"
    )
    with open(path) as f:
        lines = len(f.readlines())
    assert lines < 150, f"server.py is {lines} lines (should be <150)"
    print(f"  5. server.py is {lines} lines (< 150): PASS")


def test_next_action_bug_fixes():
    """6. next_action.py contains bug fix comments."""
    path = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "spark", "routes", "next_action.py"
    )
    with open(path) as f:
        content = f.read()
    assert "Bug #8" in content or "#8" in content, "Missing Bug #8 reference"
    assert "Bug #9" in content or "#9" in content, "Missing Bug #9 reference"
    assert "Bug #10" in content or "#10" in content, "Missing Bug #10 reference"
    print("  6. Bug fix references present: PASS")


def test_support_files_exist():
    """7. validate_action.py and action_review.py exist."""
    base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    files = [
        "spark/tasks/validate_action.py",
        "spark/tasks/action_review.py",
    ]
    missing = [f for f in files if not os.path.exists(os.path.join(base, f))]
    assert not missing, f"Missing: {missing}"
    print("  7. Support files exist: PASS")


def test_import_paths():
    """8. Import paths use spark.tasks or relative (not bare tasks.)."""
    base = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "spark"
    )
    issues = []
    for dirpath, _, filenames in os.walk(base):
        for fname in filenames:
            if not fname.endswith(".py"):
                continue
            fpath = os.path.join(dirpath, fname)
            with open(fpath) as f:
                for i, line in enumerate(f, 1):
                    # Check for bare `from tasks.` (not `from .` or `from spark.tasks.`)
                    stripped = line.strip()
                    if stripped.startswith("from tasks.") and not stripped.startswith("from tasks import"):
                        rel_path = os.path.relpath(fpath, base)
                        issues.append(f"{rel_path}:{i}: {stripped}")
    assert not issues, f"Bare 'from tasks.' found:\n" + "\n".join(issues)
    print("  8. No bare 'from tasks.' imports: PASS")


if __name__ == "__main__":
    print("Step 7: Testing server.py split into routes...")
    tests = [
        test_route_modules_import,
        test_models_import,
        test_server_imports_routers,
        test_endpoints_exist,
        test_server_is_small,
        test_next_action_bug_fixes,
        test_support_files_exist,
        test_import_paths,
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
