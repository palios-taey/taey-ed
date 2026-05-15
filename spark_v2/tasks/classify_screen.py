"""Classification entry point for spark_v2."""

from __future__ import annotations


def claude_cli_classify(tree: dict, screenshot: str | None) -> dict:
    # TODO Phase C7: replace placeholder with Claude-only classification path.
    _ = tree
    _ = screenshot
    return {
        "screen_type": "UNKNOWN",
        "expected_next": [],
        "extract": None,
        "todo": "Phase C7",
    }
