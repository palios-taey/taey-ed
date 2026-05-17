from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.tasks.compute_tree_hash import compute_tree_hash
from spark_v2.learning import cache as cache_mod
from spark_v2.routes import next_action
from spark_v2.tasks import consultation_request, knowledge_loader
from spark_v2.tasks.prompt_codex import prune_ax_tree
from spark_v2.tasks.skeleton import extract_skeleton, hash_skeleton


class PollingV7PortTests(unittest.TestCase):
    def test_step_2_7_ignores_chrome_memory_usage_hash_noise(self) -> None:
        tree_a = {
            "role": "AXApplication",
            "name": "Chrome",
            "children": [
                {
                    "role": "AXTabGroup",
                    "name": "Tabs",
                    "children": [
                        {
                            "role": "AXRadioButton",
                            "name": "Tab 1 - Memory usage - 500 MB",
                        }
                    ],
                },
                {
                    "role": "AXWebArea",
                    "name": "Khan Academy",
                    "children": [
                        {"role": "AXLink", "name": "Replay video"},
                    ],
                },
            ],
        }
        tree_b = {
            "role": "AXApplication",
            "name": "Chrome",
            "children": [
                {
                    "role": "AXTabGroup",
                    "name": "Tabs",
                    "children": [
                        {
                            "role": "AXRadioButton",
                            "name": "Tab 1 - Memory usage - 800 MB",
                        }
                    ],
                },
                {
                    "role": "AXWebArea",
                    "name": "Khan Academy",
                    "children": [
                        {"role": "AXLink", "name": "Replay video"},
                    ],
                },
            ],
        }
        self.assertNotEqual(compute_tree_hash(tree_a), compute_tree_hash(tree_b))
        self.assertEqual(
            hash_skeleton(extract_skeleton(prune_ax_tree(tree_a))),
            hash_skeleton(extract_skeleton(prune_ax_tree(tree_b))),
        )
        payload = {
            "platform": "platform_a",
            "tree": tree_b,
            "client_state": {},
            "last_result": {
                "continue_loop": True,
                "screen": "VIDEO_PLAYING",
                "tree_hash_before": compute_tree_hash(tree_a),
                "tree_hash_after": compute_tree_hash(tree_b),
                "after_tree": tree_a,
            },
        }
        self.assertIsNone(next_action.step_2_7_polling_completion(payload))

    def test_step_2_7_structural_change_falls_through_to_step_5(self) -> None:
        tree_a = {
            "role": "AXApplication",
            "name": "Chrome",
            "children": [
                {
                    "role": "AXWebArea",
                    "name": "Khan Academy",
                    "children": [
                        {"role": "AXButton", "name": "Pause"},
                    ],
                }
            ],
        }
        tree_b = {
            "role": "AXApplication",
            "name": "Chrome",
            "children": [
                {
                    "role": "AXWebArea",
                    "name": "Khan Academy",
                    "children": [
                        {"role": "AXLink", "name": "Replay"},
                        {"role": "AXLink", "name": "Up next"},
                    ],
                }
            ],
        }
        self.assertNotEqual(
            hash_skeleton(extract_skeleton(prune_ax_tree(tree_a))),
            hash_skeleton(extract_skeleton(prune_ax_tree(tree_b))),
        )
        payload = {
            "platform": "platform_a",
            "tree": tree_b,
            "client_state": {},
            "last_result": {
                "continue_loop": True,
                "screen": "VIDEO_PLAYING",
                "tree_hash_before": compute_tree_hash(tree_a),
                "tree_hash_after": compute_tree_hash(tree_b),
                "after_tree": tree_a,
            },
        }
        self.assertIsNone(next_action.step_2_7_polling_completion(payload))

    def test_step_2_7_tree_changed_falls_through(self) -> None:
        payload = {
            "platform": "platform_a",
            "tree": {"role": "AXWebArea"},
            "client_state": {},
            "last_result": {
                "continue_loop": True,
                "screen": "VIDEO_PLAYING",
                "tree_hash_before": "before",
                "tree_hash_after": "after",
            },
        }
        self.assertIsNone(next_action.step_2_7_polling_completion(payload))

    def test_step_2_7_tree_unchanged_falls_through(self) -> None:
        payload = {
            "platform": "platform_a",
            "tree": {"role": "AXWebArea"},
            "client_state": {},
            "last_result": {
                "continue_loop": True,
                "screen": "VIDEO_PLAYING",
                "tree_hash_before": "same",
                "tree_hash_after": "same",
            },
        }
        self.assertIsNone(next_action.step_2_7_polling_completion(payload))

    def test_step_4_polling_continuity_reissues_video_poll(self) -> None:
        payload = {
            "platform": "platform_a",
            "tree": {"role": "AXWebArea", "children": [{"role": "AXButton", "name": "Completely unrelated"}]},
            "client_state": {},
            "last_result": {
                "continue_loop": True,
                "screen": "VIDEO_PLAYING",
                "directive_skeleton_hash": "hash_a",
            },
        }
        directive = next_action.step_4_signature_match(payload)
        self.assertEqual(directive["directive"], "execute_tree")
        self.assertEqual(directive["screen"], "VIDEO_PLAYING")
        self.assertEqual(directive["tree"]["action"], "video_poll")
        self.assertEqual(directive["skeleton_hash"], "hash_a")

    def test_step_4_polling_continuity_reissues_for_article_any_tree_shape(self) -> None:
        payload = {
            "platform": "platform_a",
            "tree": {"role": "AXWebArea", "children": [{"role": "AXStaticText", "name": "Anything at all"}]},
            "client_state": {},
            "last_result": {
                "continue_loop": True,
                "screen": "ARTICLE_READING",
                "directive_skeleton_hash": "hash_b",
            },
        }
        directive = next_action.step_4_signature_match(payload)
        self.assertEqual(directive["directive"], "execute_tree")
        self.assertEqual(directive["screen"], "ARTICLE_READING")
        self.assertEqual(directive["tree"]["action"], "video_poll")
        self.assertEqual(directive["skeleton_hash"], "hash_b")

    def test_step_4_non_polling_uses_existing_exact_hash_lookup(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            self._patch_env(tmp)
            tree = {"role": "AXWebArea", "children": [{"role": "AXButton", "name": "Continue"}]}
            skeleton_hash = next_action._current_skeleton_hash({"tree": tree})
            knowledge = knowledge_loader._empty_shell("platform_a")
            knowledge["cached_bts"][skeleton_hash] = {
                "cache_class": "DETERMINISTIC_BT",
                "bt": {"type": "action", "action": "click", "params": {"target": "Continue"}},
                "screen_type": "NAVIGATION",
            }
            knowledge_loader.save_knowledge("platform_a", knowledge)
            payload = {
                "platform": "platform_a",
                "tree": tree,
                "client_state": {},
                "last_result": {
                    "continue_loop": False,
                    "screen": "NAVIGATION",
                },
            }
            directive = next_action.step_4_signature_match(payload)
            self.assertEqual(directive["directive"], "execute_tree")
            self.assertEqual(directive["screen"], "NAVIGATION")
            self.assertEqual(directive["tree"]["action"], "click")

    def test_step_4_lenient_match_does_not_fire_without_continue_loop(self) -> None:
        payload = {
            "platform": "platform_a",
            "tree": {"role": "AXWebArea", "children": [{"role": "AXStaticText", "name": "Anything"}]},
            "client_state": {},
            "last_result": {
                "continue_loop": False,
                "screen": "VIDEO_PLAYING",
            },
        }
        self.assertIsNone(next_action.step_4_signature_match(payload))

    def _patch_env(self, temp_dir: str) -> None:
        platforms_dir = Path(temp_dir) / "platforms"
        consult_dir = Path(temp_dir) / "consults"
        patchers = [
            patch.object(knowledge_loader, "PLATFORMS_DIR", platforms_dir),
            patch.object(cache_mod, "PLATFORMS_DIR", platforms_dir),
            patch.object(consultation_request, "CONSULT_DIR", consult_dir),
            patch.object(next_action, "CONSULT_DIR", consult_dir),
        ]
        for patcher in patchers:
            patcher.start()
            self.addCleanup(patcher.stop)


if __name__ == "__main__":
    unittest.main()
