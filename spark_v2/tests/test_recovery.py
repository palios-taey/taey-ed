from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from spark_v2.learning import outcome_log
from spark_v2.recovery import request_writer, result_parser
from spark_v2.routes import next_action
from spark_v2.tasks import knowledge_loader, prompt_codex


class RecoveryTests(unittest.TestCase):
    def test_extract_trailing_json_object(self) -> None:
        payload = result_parser.extract_trailing_json_object('note\n{"ok":true,"n":1}')
        self.assertEqual(payload["ok"], True)
        self.assertEqual(payload["n"], 1)

    def test_validate_rejects_missing_deprecated_paths(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            result_parser.RECOVERY_DIR = Path(tmp) / "recovery"
            request_id = "recovery_khan_academy_1_abc123"
            req_dir = result_parser.RECOVERY_DIR / request_id
            req_dir.mkdir(parents=True, exist_ok=True)
            payload = {
                "amendment_rationale": "delta",
                "schema_version": "v2",
                "platform": {"name": "khan_academy"},
                "recovery_classification": "stale_platform_knowledge",
                "diagnosis": "stale",
                "amendments": {"deprecated_canonical_paths": [], "screen_patterns": {}},
                "extraction_hints": {},
                "research_confidence": {"overall": "medium", "sources_count": 1, "notes": "", "unknown_fields": []},
            }
            ok, errors = result_parser.validate_and_merge_recovery_result(payload, "khan_academy", request_id)
            self.assertFalse(ok)
            self.assertTrue(any("deprecated_canonical_paths" in item for item in errors))

    def test_request_writer_substitutes_template_fields(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            request_writer.RECOVERY_DIR = Path(tmp) / "recovery"
            request_writer.PROMPT_TEMPLATE_PATH = Path(tmp) / "prompt.md"
            request_writer.PROMPT_TEMPLATE_PATH.write_text("A {PLATFORM_DISPLAY_NAME} {FAILED_PROVISIONAL_ATTEMPTS}")
            with patch("spark_v2.recovery.request_writer.subprocess.run") as mock_run:
                mock_run.return_value.returncode = 0
                mock_run.return_value.stderr = ""
                request_id = request_writer.create_request(
                    {
                        "platform": "khan_academy",
                        "platform_display_name": "Khan Academy",
                        "provisional": {"_recovery_entries": [], "_meta": {"failed_attempts": [{"x": 1}]}}
                    }
                )
            prompt = (request_writer.RECOVERY_DIR / request_id / "prompt.txt").read_text()
            self.assertIn("Khan Academy", prompt)
            self.assertIn('"x": 1', prompt)

    def test_render_knowledge_masks_deprecated_paths(self) -> None:
        knowledge = {
            "platform": {"name": "khan_academy", "display_name": "Khan Academy"},
            "global": {"completion_indicators": [], "advancement_link_patterns": []},
            "screen_patterns": {"EXERCISE": {"submit_button_label": "Check", "provenance": {"source": "discovery"}}},
            "never_clicks_platform": [],
            "widget_classes": {},
            "cached_bts": {},
        }
        provisional = {
            "_recovery_entries": [
                {
                    "entry_id": "r1",
                    "amendments": {
                        "deprecated_canonical_paths": ["screen_patterns/EXERCISE/submit_button_label"],
                        "screen_patterns": {
                            "EXERCISE": {
                                "submit_button_label": {
                                    "value": "Submit",
                                    "provenance": {"source": "recovery"}
                                }
                            }
                        },
                    },
                }
            ]
        }
        canonical, provisional_block = prompt_codex.render_knowledge_for_worker(knowledge, provisional)
        self.assertNotIn("Check", canonical)
        self.assertIn("submit_button_label", provisional_block)
        self.assertIn("overrides=['screen_patterns/EXERCISE/submit_button_label']", provisional_block)

    def test_atomic_graduation_rolls_back_on_partial_failure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            knowledge_loader.PLATFORMS_DIR = Path(tmp) / "platforms"
            platform_dir = knowledge_loader.PLATFORMS_DIR / "khan_academy"
            platform_dir.mkdir(parents=True, exist_ok=True)
            knowledge = knowledge_loader._empty_shell("khan_academy")
            provisional = knowledge_loader._empty_provisional_shell("khan_academy")
            provisional["_recovery_entries"] = [
                {
                    "entry_id": "r1",
                    "request_id": "req1",
                    "amendments": {
                        "deprecated_canonical_paths": [],
                        "screen_patterns": {
                            "EXERCISE": {
                                "submit_button_label": {
                                    "value": "Submit",
                                    "provenance": {"source": "recovery", "validated_step2": False, "validated_step2_at": None},
                                }
                            }
                        },
                    },
                }
            ]
            (platform_dir / "knowledge.json").write_text(json.dumps(knowledge))
            (platform_dir / "provisional_knowledge.json").write_text(json.dumps(provisional))
            original = json.loads((platform_dir / "knowledge.json").read_text())

            real_atomic = knowledge_loader.atomic_write_json
            call_count = {"n": 0}

            def flaky(path, data, indent=2):
                call_count["n"] += 1
                if call_count["n"] == 2:
                    raise RuntimeError("boom")
                return real_atomic(path, data, indent=indent)

            with patch("spark_v2.tasks.knowledge_loader.atomic_write_json", side_effect=flaky):
                with self.assertRaises(RuntimeError):
                    knowledge_loader.graduate_active_recovery_entries("khan_academy", "consult_1")

            self.assertEqual(json.loads((platform_dir / "knowledge.json").read_text()), original)

    def test_step2_success_logs_and_graduates(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            knowledge_loader.PLATFORMS_DIR = Path(tmp) / "platforms"
            outcome_log.OUTCOMES_DIR = Path(tmp) / "outcomes"
            next_action.CONSULT_DIR = Path(tmp) / "consults"
            platform_dir = knowledge_loader.PLATFORMS_DIR / "khan_academy"
            consult_dir = next_action.CONSULT_DIR / "consult_1"
            platform_dir.mkdir(parents=True, exist_ok=True)
            consult_dir.mkdir(parents=True, exist_ok=True)
            knowledge = knowledge_loader._empty_shell("khan_academy")
            provisional = knowledge_loader._empty_provisional_shell("khan_academy")
            provisional["_recovery_entries"] = [
                {
                    "entry_id": "r1",
                    "request_id": "req1",
                    "amendments": {
                        "deprecated_canonical_paths": [],
                        "screen_patterns": {
                            "ARTICLE": {
                                "mark_complete_button": {
                                    "value": "Continue",
                                    "provenance": {"source": "recovery", "validated_step2": False, "validated_step2_at": None},
                                }
                            }
                        },
                    },
                }
            ]
            (platform_dir / "knowledge.json").write_text(json.dumps(knowledge))
            (platform_dir / "provisional_knowledge.json").write_text(json.dumps(provisional))
            (consult_dir / "metadata.json").write_text(json.dumps({"tier": 1}))
            payload = {
                "platform": "khan_academy",
                "tree": {"role": "AXWebArea"},
                "client_state": {"active_consultation_id": "consult_1", "course_id": "course-a"},
                "last_result": {
                    "success": True,
                    "continue_loop": False,
                    "screen": "ARTICLE",
                    "tree_hash_before": "a",
                    "tree_hash_after": "b",
                    "directive_skeleton_hash": "sk1",
                    "action": "click",
                },
            }
            result = next_action.step_2_validate_previous_action(payload)
            self.assertIsNone(result)
            updated_knowledge = json.loads((platform_dir / "knowledge.json").read_text())
            self.assertIn("ARTICLE", updated_knowledge["screen_patterns"])
            outcomes = outcome_log.get_platform_outcomes("khan_academy")
            event_kinds = [item["event_kind"] for item in outcomes]
            self.assertIn("execution", event_kinds)
            self.assertIn("graduation", event_kinds)


if __name__ == "__main__":
    unittest.main()
