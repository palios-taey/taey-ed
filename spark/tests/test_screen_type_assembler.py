from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from spark.tasks import screen_session
from spark.tasks.screen_type_assembler import (
    ScreenTypeAssemblerError,
    assemble_worker_prompt,
    validate_worker_bt_response,
)


class ScreenTypeAssemblerTests(unittest.TestCase):
    def test_assemble_uses_exact_yaml_for_known_type(self) -> None:
        prompt, meta = assemble_worker_prompt(
            tree={"role": "AXWebArea", "children": []},
            platform="khan_academy",
            consultation_id="c1",
            screen_type="VIDEO",
            kb_chunks=[],
        )
        self.assertIn("screen_type: VIDEO", prompt)
        self.assertNotIn("UNKNOWN Classification Guide", prompt)
        self.assertEqual(meta["artifact_screen_type"], "VIDEO")

    def test_assemble_uses_unknown_guide_for_unknown_type(self) -> None:
        prompt, meta = assemble_worker_prompt(
            tree={"role": "AXWebArea", "children": []},
            platform="khan_academy",
            consultation_id="c2",
            screen_type="UNKNOWN",
            kb_chunks=[],
        )
        self.assertIn("UNKNOWN Classification Guide", prompt)
        self.assertEqual(meta["artifact_kind"], "unknown_guide")

    def test_validate_rejects_recipe_drift_and_banned_contains(self) -> None:
        bt = {
            "tree": {
                "type": "sequence",
                "children": [
                    {"type": "action", "action": "find_all", "params": {"role": "AXComboBox"}},
                    {
                        "type": "action",
                        "action": "find_and_click",
                        "params": {"target": "Check", "match_mode": "contains"},
                    },
                ],
            },
            "screen_type": "EXERCISE_DROPDOWN",
            "expected_next": ["TRANSITION"],
            "extract": None,
        }
        with self.assertRaises(ScreenTypeAssemblerError):
            validate_worker_bt_response(bt, platform="khan_academy", screen_type="EXERCISE_DROPDOWN")

    def test_screen_session_rolls_old_attempts_to_archive(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(screen_session, "_BASE", Path(tmp)):
                for idx in range(8):
                    screen_session.record_attempt(
                        "platform_a",
                        "hash1",
                        bt_actions=[f"a{idx}"],
                        outcome="failed",
                        detail=str(idx),
                        author="worker",
                    )
                live = json.loads((Path(tmp) / "platform_a" / "hash1.json").read_text(encoding="utf-8"))
                archive_path = Path(tmp) / "platform_a" / "archive" / "hash1.jsonl"
                self.assertEqual(len(live["attempts"]), 6)
                self.assertTrue(archive_path.exists())
                archive_lines = archive_path.read_text(encoding="utf-8").strip().splitlines()
                self.assertGreaterEqual(len(archive_lines), 1)
                first_roll = json.loads(archive_lines[0])
                self.assertEqual(first_roll["reason"], "live_window_roll")
                self.assertEqual(len(first_roll["attempts"]), 1)


if __name__ == "__main__":
    unittest.main()
