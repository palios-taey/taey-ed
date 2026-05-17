from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from spark_v2.learning import cache, invalidation, outcome_log, promotion
from spark_v2.learning.cache import _determine_cache_class, load_cached_bt, store_cached_bt
from spark_v2.learning.cleanliness import is_clean
from spark_v2.learning.outcome_log import get_platform_outcomes, log_outcome
from spark_v2.learning.provenance import compute_provenance_hash


class PromotionMathTests(unittest.TestCase):
    def test_lower_ci_zero_successes(self) -> None:
        self.assertEqual(promotion.lower_95_ci_clopper_pearson(0, 5), 0.0)

    def test_lower_ci_five_of_five_below_threshold(self) -> None:
        self.assertLess(promotion.lower_95_ci_clopper_pearson(5, 5), 0.95)

    def test_lower_ci_seventy_two_of_seventy_two_above_floor(self) -> None:
        self.assertGreaterEqual(promotion.lower_95_ci_clopper_pearson(72, 72), 0.95)

    def test_is_clean_true(self) -> None:
        self.assertTrue(
            is_clean(
                {
                    "success": True,
                    "tier": 0,
                    "wrong_answer_retry": False,
                    "worker_fallback": False,
                    "step2_validated": True,
                }
            )
        )

    def test_is_clean_false_for_tier_one(self) -> None:
        self.assertFalse(
            is_clean(
                {
                    "success": True,
                    "tier": 1,
                    "wrong_answer_retry": False,
                    "worker_fallback": False,
                    "step2_validated": True,
                }
            )
        )

    def test_is_clean_false_when_step2_missing(self) -> None:
        self.assertFalse(
            is_clean(
                {
                    "success": True,
                    "tier": 0,
                    "wrong_answer_retry": False,
                    "worker_fallback": False,
                    "step2_validated": None,
                }
            )
        )

    def test_compute_provenance_hash_order_independent(self) -> None:
        self.assertEqual(
            compute_provenance_hash(["a", "b"]),
            compute_provenance_hash(["b", "a"]),
        )

    def test_determine_cache_class_navigation_override(self) -> None:
        self.assertEqual(_determine_cache_class("NAVIGATION"), "NO_CACHE")

    def test_determine_cache_class_video(self) -> None:
        self.assertEqual(_determine_cache_class("VIDEO"), "DETERMINISTIC_BT")

    def test_determine_cache_class_exercise_variant(self) -> None:
        self.assertEqual(_determine_cache_class("EXERCISE_NUMERIC"), "PROCEDURAL_TEMPLATE")

    def test_outcome_log_roundtrip(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            with patch.object(outcome_log, "OUTCOMES_DIR", Path(temp_dir)):
                expected = log_outcome(
                    "platform_a",
                    "VIDEO",
                    "hash_a",
                    "consult_a",
                    "course_a",
                    {"type": "sequence", "children": []},
                    True,
                    0,
                    False,
                    False,
                    True,
                )
                records = get_platform_outcomes("platform_a")
                self.assertEqual(records, [expected])

    def test_attempt_promotion_fails_cross_unit_holdout(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            self._patch_learning_dirs(temp_dir)
            for index in range(5):
                log_outcome(
                    "platform_a",
                    "VIDEO",
                    "hash_a",
                    f"consult_{index}",
                    "course_a",
                    {"type": "action", "action": "wait", "params": {"seconds": 1.0}},
                    True,
                    0,
                    False,
                    False,
                    True,
                )
            self.assertIsNone(promotion.attempt_promotion("platform_a", "hash_a", "VIDEO"))

    def test_attempt_promotion_fails_lower_ci_with_five_of_five(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            self._patch_learning_dirs(temp_dir)
            for index in range(5):
                log_outcome(
                    "platform_a",
                    "VIDEO",
                    "hash_a",
                    f"consult_{index}",
                    f"course_{index % 2}",
                    {"type": "action", "action": "wait", "params": {"seconds": 1.0}},
                    True,
                    0,
                    False,
                    False,
                    True,
                )
            self.assertIsNone(promotion.attempt_promotion("platform_a", "hash_a", "VIDEO"))

    def test_attempt_promotion_succeeds_with_sufficient_clean_history(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            self._patch_learning_dirs(temp_dir)
            for index in range(73):
                plan = {"type": "action", "action": "wait", "params": {"seconds": 1.0}}
                if index % 5 == 0:
                    plan = {"type": "action", "action": "wait", "params": {"seconds": 1.0}}
                log_outcome(
                    "platform_a",
                    "VIDEO",
                    "hash_a",
                    f"consult_{index}",
                    f"course_{index % 3}",
                    plan,
                    True,
                    0,
                    False,
                    False,
                    True,
                )
            entry = promotion.attempt_promotion("platform_a", "hash_a", "VIDEO")
            self.assertIsNotNone(entry)
            assert entry is not None
            self.assertIn("provenance_hash", entry)
            self.assertIn("provenance_consults", entry)
            self.assertGreater(len(entry["provenance_consults"]), 0)
            self.assertEqual(entry["cache_class"], "DETERMINISTIC_BT")
            self.assertEqual(load_cached_bt("platform_a", "hash_a")["provenance_hash"], entry["provenance_hash"])

    def test_invalidation_deletes_after_two_failures(self) -> None:
        knowledge = {
            "cached_bts": {
                "hash_a": {
                    "consecutive_failures_post_promotion": 0,
                    "last_validated_at": "2026-05-17T00:00:00+00:00",
                }
            }
        }
        invalidation.on_post_promotion_failure("hash_a", "platform_a", knowledge)
        self.assertIn("hash_a", knowledge["cached_bts"])
        invalidation.on_post_promotion_failure("hash_a", "platform_a", knowledge)
        self.assertNotIn("hash_a", knowledge["cached_bts"])

    def _patch_learning_dirs(self, temp_dir: str) -> unittest.mock._patch:
        platforms_dir = Path(temp_dir) / "platforms"
        outcomes_dir = Path(temp_dir) / "outcomes"
        patchers = [
            patch.object(outcome_log, "OUTCOMES_DIR", outcomes_dir),
            patch.object(cache, "PLATFORMS_DIR", platforms_dir),
        ]
        for patcher in patchers:
            patcher.start()
            self.addCleanup(patcher.stop)


if __name__ == "__main__":
    unittest.main()
