from __future__ import annotations

import json
import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
RECIPES_DIR = ROOT / "spark_v2" / "prompts" / "recipes"
JSON_BLOCK_RE = re.compile(r"```json\n(.*?)\n```", re.DOTALL)


def _iter_send_to_llm_nodes(node: object):
    if isinstance(node, dict):
        if node.get("action") == "send_to_llm":
            yield node
        for value in node.values():
            yield from _iter_send_to_llm_nodes(value)
    elif isinstance(node, list):
        for item in node:
            yield from _iter_send_to_llm_nodes(item)


class RecipeSendToLlmHasQuestionTests(unittest.TestCase):
    def test_every_send_to_llm_node_has_nonempty_question(self) -> None:
        violations: list[str] = []
        for path in sorted(RECIPES_DIR.glob("*.md")):
            text = path.read_text(encoding="utf-8")
            for block_index, raw_block in enumerate(JSON_BLOCK_RE.findall(text), start=1):
                parsed = json.loads(raw_block)
                for node in _iter_send_to_llm_nodes(parsed):
                    params = node.get("params")
                    question = params.get("question") if isinstance(params, dict) else None
                    if not isinstance(question, str) or not question.strip():
                        violations.append(
                            f"{path} block {block_index}: send_to_llm missing non-empty question\n{raw_block}"
                        )
        self.assertEqual([], violations, "\n\n".join(violations))


if __name__ == "__main__":
    unittest.main()
