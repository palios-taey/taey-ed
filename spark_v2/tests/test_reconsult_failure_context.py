from __future__ import annotations

import unittest

from spark_v2.tasks.build_consultation_prompt import build_consultation_prompt


class ReconsultFailureContextTests(unittest.TestCase):
    def test_tier1_prompt_includes_failure_block_and_exact_rule(self) -> None:
        prompt = build_consultation_prompt(
            consultation_id="consult_test",
            platform="khan_academy",
            tree={"role": "AXWebArea", "children": [{"role": "AXLink", "name": "Resume"}]},
            tier=1,
            previous_bt={
                "type": "action",
                "action": "find_and_click",
                "params": {"target": "Resume", "role": "AXLink", "strategy": "mouse_click", "match_mode": "exact"},
            },
            previous_response={"action": "behavior_tree (failure)", "error": "", "tree_changed": False},
        )
        self.assertIn("STEP 2.5: PREVIOUS ATTEMPT FAILED", prompt)
        self.assertIn("tree_changed: false", prompt)
        self.assertIn("Always use match_mode: exact.", prompt)
        self.assertIn("NEVER use match_mode contains.", prompt)


if __name__ == "__main__":
    unittest.main()
