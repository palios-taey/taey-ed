from __future__ import annotations

import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
PROMPTS_DIR = ROOT / "spark_v2" / "prompts"
SOLVE_PROMPT_PATH = Path("/home/user/taey-ed/consultations/SOLVE_PROMPT_v1.md")
FORBIDDEN_LITERALS = (
    'match_mode": "contains"',
    'match_mode:"contains"',
    "match_mode: contains",
)


class NoMatchModeContainsTests(unittest.TestCase):
    def test_prompt_markdown_has_no_contains_match_mode(self) -> None:
        paths = sorted(PROMPTS_DIR.rglob("*.md"))
        self.assertTrue(paths, "expected spark_v2 prompt markdown files")
        self.assertTrue(SOLVE_PROMPT_PATH.exists(), f"missing {SOLVE_PROMPT_PATH}")
        paths.append(SOLVE_PROMPT_PATH)
        violations: list[str] = []
        for path in paths:
            text = path.read_text(encoding="utf-8")
            for literal in FORBIDDEN_LITERALS:
                if literal in text:
                    violations.append(f"{path}: {literal}")
        self.assertEqual([], violations, "\n".join(violations))
