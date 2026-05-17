from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from spark_v2.learning import cache as cache_mod, outcome_log
from spark_v2.routes import next_action
from spark_v2.tasks import consultation_request, knowledge_loader
from spark_v2.tasks import prompt_codex


class CacheRoutingTests(unittest.TestCase):
    def test_cache_miss_returns_none(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            self._patch_env(tmp)
            payload = {"platform": "platform_a", "tree": {"role": "AXWebArea"}, "client_state": {}}
            self.assertIsNone(next_action.step_4_signature_match(payload))

    def test_deterministic_cache_hit_returns_execute_tree(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            self._patch_env(tmp)
            tree = {"role": "AXWebArea", "children": [{"role": "AXButton", "name": "Continue"}]}
            skeleton_hash = next_action._current_skeleton_hash({"tree": tree})
            knowledge = knowledge_loader._empty_shell("platform_a")
            knowledge["cached_bts"][skeleton_hash] = {
                "cache_class": "DETERMINISTIC_BT",
                "bt": {"type": "action", "action": "click", "params": {"target": "Continue"}},
                "screen_type": "ARTICLE",
            }
            knowledge_loader.save_knowledge("platform_a", knowledge)
            directive = next_action.step_4_signature_match({"platform": "platform_a", "tree": tree, "client_state": {}})
            self.assertEqual(directive["directive"], "execute_tree")
            self.assertEqual(directive["screen"], "ARTICLE")
            self.assertEqual(directive["tree"]["action"], "click")
            events = outcome_log.get_platform_outcomes("platform_a")
            self.assertEqual(events[-1]["event_kind"], "cache_hit")

    def test_procedural_template_spawns_consult_with_cache_steering(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            self._patch_env(tmp)
            tree = {"role": "AXWebArea", "children": [{"role": "AXStaticText", "name": "2 + 2"}]}
            skeleton_hash = next_action._current_skeleton_hash({"tree": tree})
            knowledge = knowledge_loader._empty_shell("platform_a")
            knowledge["cached_bts"][skeleton_hash] = {
                "cache_class": "PROCEDURAL_TEMPLATE",
                "bt": {"type": "sequence", "children": [{"type": "action", "action": "send_to_llm", "params": {}}]},
                "screen_type": "EXERCISE_NUMERIC",
            }
            knowledge_loader.save_knowledge("platform_a", knowledge)
            payload = {
                "platform": "platform_a",
                "tree": tree,
                "client_state": {"course_id": "course_a"},
                "screenshot_b64": "aGVsbG8=",
            }
            directive = next_action.step_4_signature_match(payload)
            self.assertEqual(directive["directive"], "consulting")
            consult_id = directive["consultation_id"]
            prompt = json.loads((consultation_request.CONSULT_DIR / consult_id / "prompt.json").read_text())
            self.assertEqual(prompt["cache_steering_hash"], skeleton_hash)
            self.assertEqual(prompt["cache_steering_entry"]["cache_class"], "PROCEDURAL_TEMPLATE")
            self.assertIn(
                "Reuse the structural template",
                prompt_codex.format_cache_steering(prompt["cache_steering_entry"], prompt["cache_steering_hash"]),
            )

    def test_no_cache_falls_through(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            self._patch_env(tmp)
            tree = {"role": "AXWebArea", "children": []}
            skeleton_hash = next_action._current_skeleton_hash({"tree": tree})
            knowledge = knowledge_loader._empty_shell("platform_a")
            knowledge["cached_bts"][skeleton_hash] = {
                "cache_class": "NO_CACHE",
                "bt": None,
                "screen_type": "NAVIGATION",
            }
            knowledge_loader.save_knowledge("platform_a", knowledge)
            self.assertIsNone(next_action.step_4_signature_match({"platform": "platform_a", "tree": tree, "client_state": {}}))

    def test_two_cache_validation_failures_delete_entry(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            self._patch_env(tmp)
            tree = {"role": "AXWebArea"}
            skeleton_hash = next_action._current_skeleton_hash({"tree": tree})
            knowledge = knowledge_loader._empty_shell("platform_a")
            knowledge["cached_bts"][skeleton_hash] = {
                "cache_class": "DETERMINISTIC_BT",
                "bt": {"type": "action", "action": "click", "params": {"target": "Continue"}},
                "screen_type": "ARTICLE",
                "consecutive_failures_post_promotion": 0,
                "last_validated_at": "2026-05-17T00:00:00+00:00",
            }
            knowledge_loader.save_knowledge("platform_a", knowledge)
            payload = {
                "platform": "platform_a",
                "tree": tree,
                "client_state": {},
                "last_result": {
                    "success": False,
                    "continue_loop": False,
                    "screen": "ARTICLE",
                    "tree_hash_before": "a",
                    "tree_hash_after": "a",
                    "directive_skeleton_hash": skeleton_hash,
                    "action": "click_failed",
                    "failed_bt": {"type": "action", "action": "click"},
                },
            }
            next_action.step_2_validate_previous_action(payload)
            next_action.step_2_validate_previous_action(payload)
            updated = knowledge_loader.load_knowledge("platform_a")
            self.assertNotIn(skeleton_hash, updated["cached_bts"])

    def _patch_env(self, temp_dir: str) -> None:
        platforms_dir = Path(temp_dir) / "platforms"
        outcomes_dir = Path(temp_dir) / "outcomes"
        consult_dir = Path(temp_dir) / "consults"
        patchers = [
            patch.object(knowledge_loader, "PLATFORMS_DIR", platforms_dir),
            patch.object(cache_mod, "PLATFORMS_DIR", platforms_dir),
            patch.object(outcome_log, "OUTCOMES_DIR", outcomes_dir),
            patch.object(consultation_request, "CONSULT_DIR", consult_dir),
            patch.object(next_action, "CONSULT_DIR", consult_dir),
        ]
        for patcher in patchers:
            patcher.start()
            self.addCleanup(patcher.stop)


if __name__ == "__main__":
    unittest.main()
