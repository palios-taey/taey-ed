from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from spark.models import NextActionRequest
from spark.routes import next_action
from spark.tasks import knowledge_loader, variant_cache
import json


class VerifiedBtBypassTests(unittest.TestCase):
    def test_get_verified_bt_template_threshold(self) -> None:
        knowledge = {
            "screen_types": {
                "EXERCISE": {
                    "subtypes": [
                        {
                            "name": "dropdown",
                            "operational_notes": [
                                {
                                    "verified_count": 0,
                                    "bt_template": {"tree": {"type": "action", "action": "wait"}},
                                },
                                {
                                    "verified_count": 1,
                                    "bt_template": {"tree": {"type": "action", "action": "click"}},
                                },
                            ],
                        }
                    ]
                }
            }
        }
        self.assertIsNone(knowledge_loader.get_verified_bt_template(knowledge, "EXERCISE_DROPDOWN", min_verified=2))
        template = knowledge_loader.get_verified_bt_template(knowledge, "EXERCISE_DROPDOWN", min_verified=1)
        self.assertIsNotNone(template)
        self.assertEqual(template["tree"]["action"], "click")

    def test_is_non_deterministic_flips_with_verified_template(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            platforms_dir = Path(tmp) / "platforms"
            variant_cache.VARIANT_BTS_DIR = Path(tmp) / "variant_bts"
            variant_cache.HASH_INDEX_DIR = Path(tmp) / "hash_index"
            with patch.object(knowledge_loader, "_platforms_dir", return_value=platforms_dir):
                self._write_knowledge(
                    platforms_dir,
                    verified_count=0,
                )
                self.assertTrue(variant_cache.is_non_deterministic("platform_a", "EXERCISE_DROPDOWN"))
                self._write_knowledge(
                    platforms_dir,
                    verified_count=1,
                )
                self.assertFalse(variant_cache.is_non_deterministic("platform_a", "EXERCISE_DROPDOWN"))

    def test_next_action_reuses_verified_operational_note_template(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            platforms_dir = Path(tmp) / "platforms"
            variant_cache.VARIANT_BTS_DIR = Path(tmp) / "variant_bts"
            variant_cache.HASH_INDEX_DIR = Path(tmp) / "hash_index"
            with patch.object(knowledge_loader, "_platforms_dir", return_value=platforms_dir):
                self._write_knowledge(
                    platforms_dir,
                    verified_count=1,
                )
                request = NextActionRequest(
                    session_id="s1",
                    platform="platform_a",
                    tree={"role": "AXWebArea", "children": [{"role": "AXComboBox", "name": "Select"}]},
                    screenshot_b64="iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+a7U8AAAAASUVORK5CYII=",
                    client_state={"course_id": "course_a"},
                )
                with patch.object(next_action, "load_yaml", return_value={}):
                    with patch("spark.tasks.flash_classify.classify_screen_flash", return_value={
                        "variant": "EXERCISE_DROPDOWN",
                        "screen_type": "EXERCISE",
                        "confidence_note": "test",
                    }):
                        with patch.object(next_action, "_build_screen_directive", side_effect=AssertionError("should bypass consult")):
                            result = next_action.next_action(request)
                self.assertEqual(result["directive"], "execute_tree")
                self.assertEqual(result["screen"], "EXERCISE_DROPDOWN")
                self.assertEqual(result["tree"]["action"], "select_dropdown_option")

    def test_mark_variant_validated_increments_operational_note_verified_count(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            platforms_dir = Path(tmp) / "platforms"
            variant_cache.VARIANT_BTS_DIR = Path(tmp) / "variant_bts"
            variant_cache.HASH_INDEX_DIR = Path(tmp) / "hash_index"
            with patch.object(knowledge_loader, "_platforms_dir", return_value=platforms_dir):
                knowledge_path = self._write_knowledge(
                    platforms_dir,
                    verified_count=1,
                )
                variant_cache.mark_variant_validated("platform_a", "EXERCISE_DROPDOWN")
                updated = json.loads(knowledge_path.read_text())
                note = updated["screen_types"]["EXERCISE"]["subtypes"][0]["operational_notes"][0]
                self.assertEqual(note["verified_count"], 2)
                self.assertIn("last_verified_at", note)

    def _write_knowledge(self, platforms_dir: Path, verified_count: int) -> Path:
        platform_dir = platforms_dir / "platform_a"
        platform_dir.mkdir(parents=True, exist_ok=True)
        payload = {
            "platform": {"name": "platform_a"},
            "schema_version": "1",
            "global": {},
            "screen_types": {
                "EXERCISE": {
                    "subtypes": [
                        {
                            "name": "dropdown",
                            "operational_notes": [
                                {
                                    "discovered_at": "2026-05-18T00:00:00Z",
                                    "note": "verified dropdown template",
                                    "verified_count": verified_count,
                                    "bt_template": {
                                        "tree": {
                                            "type": "action",
                                            "action": "select_dropdown_option",
                                            "params": {"target": "A"},
                                        },
                                        "extract": {"scope": "web_area"},
                                        "expected_next": ["EXERCISE_FEEDBACK"],
                                    },
                                }
                            ],
                        }
                    ]
                }
            },
        }
        knowledge_path = platform_dir / "knowledge.json"
        knowledge_path.write_text(json.dumps(payload), encoding="utf-8")
        return knowledge_path


if __name__ == "__main__":
    unittest.main()
