from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from spark_v2.learning import outcome_log
from spark_v2.routes import next_action
from spark_v2.tasks import consultation_request, knowledge_loader


class PollingCompletionTests(unittest.TestCase):
    def test_no_completion_signal_defaults_to_repoll(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            self._patch_env(tmp)
            knowledge_loader.save_knowledge("platform_a", knowledge_loader._empty_shell("platform_a"))
            payload = {
                "platform": "platform_a",
                "tree": {"role": "AXWebArea"},
                "client_state": {},
                "last_result": {
                    "success": True,
                    "continue_loop": True,
                    "action": "behavior_tree (success)",
                    "tree_hash_before": "same",
                    "tree_hash_after": "same",
                },
            }
            directive = next_action.step_2_7_polling_completion(payload)
            self.assertEqual(directive["directive"], "execute_tree")
            self.assertEqual(directive["tree"]["action"], "video_poll")
            self.assertEqual(outcome_log.get_platform_outcomes("platform_a")[-1]["event_kind"], "video_polling_no_signal")

    def test_completion_signal_match_spawns_advancement_consult(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            self._patch_env(tmp)
            knowledge = knowledge_loader._empty_shell("platform_a")
            knowledge["global"]["video_completion_signal"] = {
                "pattern_type": "other",
                "pattern_value": "Replay",
            }
            knowledge_loader.save_knowledge("platform_a", knowledge)
            payload = {
                "platform": "platform_a",
                "tree": {"role": "AXWebArea", "children": [{"role": "AXButton", "name": "Replay"}]},
                "client_state": {"course_id": "course_a"},
                "screenshot_b64": "aGVsbG8=",
                "last_result": {
                    "success": True,
                    "continue_loop": True,
                    "action": "video_poll",
                    "tree_hash_before": "same",
                    "tree_hash_after": "same",
                },
            }
            directive = next_action.step_2_7_polling_completion(payload)
            self.assertEqual(directive["directive"], "consulting")
            consult_id = directive["consultation_id"]
            self.assertEqual(
                consultation_request.poll_consultation(consult_id)["metadata"]["screen_type_hint"],
                "VIDEO_COMPLETE_ADVANCEMENT",
            )

    def test_completion_signal_absent_repolls_video(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            self._patch_env(tmp)
            knowledge = knowledge_loader._empty_shell("platform_a")
            knowledge["global"]["video_completion_signal"] = {
                "pattern_type": "other",
                "pattern_value": "Replay",
            }
            knowledge_loader.save_knowledge("platform_a", knowledge)
            payload = {
                "platform": "platform_a",
                "tree": {"role": "AXWebArea", "children": [{"role": "AXButton", "name": "Pause"}]},
                "client_state": {"course_id": "course_a"},
                "last_result": {
                    "success": True,
                    "continue_loop": True,
                    "action": "video_poll",
                    "tree_hash_before": "same",
                    "tree_hash_after": "same",
                },
            }
            directive = next_action.step_2_7_polling_completion(payload)
            self.assertEqual(directive["directive"], "execute_tree")
            self.assertEqual(directive["tree"]["action"], "video_poll")
            self.assertEqual(outcome_log.get_platform_outcomes("platform_a")[-1]["event_kind"], "video_still_polling")

    def test_provisional_only_signal_repolls_when_indicator_does_not_match(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            self._patch_env(tmp)
            knowledge_loader.save_knowledge("platform_a", knowledge_loader._empty_shell("platform_a"))
            provisional = knowledge_loader._empty_provisional_shell("platform_a")
            provisional["global"]["video_completion_signal"] = {
                "pattern_type": "other",
                "pattern_value": "Replay",
            }
            knowledge_loader.merge_provisional_to_global("platform_a", provisional)
            payload = {
                "platform": "platform_a",
                "tree": {"role": "AXWebArea", "children": [{"role": "AXButton", "name": "Pause"}]},
                "client_state": {"course_id": "course_a"},
                "last_result": {
                    "success": True,
                    "continue_loop": True,
                    "action": "video_poll",
                    "tree_hash_before": "same",
                    "tree_hash_after": "same",
                },
            }
            directive = next_action.step_2_7_polling_completion(payload)
            self.assertEqual(directive["directive"], "execute_tree")
            self.assertEqual(directive["tree"]["action"], "video_poll")
            self.assertEqual(outcome_log.get_platform_outcomes("platform_a")[-1]["event_kind"], "video_still_polling")

    def test_sixty_identical_polls_emits_user_input_needed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            self._patch_env(tmp)
            knowledge_loader.save_knowledge("platform_a", knowledge_loader._empty_shell("platform_a"))
            payload = {
                "platform": "platform_a",
                "tree": {"role": "AXWebArea"},
                "client_state": {"consecutive_video_polls": 59},
                "last_result": {
                    "success": True,
                    "continue_loop": True,
                    "action": "video_poll",
                    "tree_hash_before": "same",
                    "tree_hash_after": "same",
                },
            }
            directive = next_action.step_2_7_polling_completion(payload)
            self.assertEqual(directive["directive"], "user_input_needed")
            self.assertEqual(directive["reason"], "video_polling_stuck")

    def _patch_env(self, temp_dir: str) -> None:
        platforms_dir = Path(temp_dir) / "platforms"
        outcomes_dir = Path(temp_dir) / "outcomes"
        consult_dir = Path(temp_dir) / "consults"
        patchers = [
            patch.object(knowledge_loader, "PLATFORMS_DIR", platforms_dir),
            patch.object(outcome_log, "OUTCOMES_DIR", outcomes_dir),
            patch.object(consultation_request, "CONSULT_DIR", consult_dir),
            patch.object(next_action, "CONSULT_DIR", consult_dir),
        ]
        for patcher in patchers:
            patcher.start()
            self.addCleanup(patcher.stop)


if __name__ == "__main__":
    unittest.main()
