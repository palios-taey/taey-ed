"""Prompt assembly for spark_v2."""

from __future__ import annotations

import json
from pathlib import Path

from spark_v2.tasks.knowledge_loader import load_knowledge, load_provisional

BASE_DIR = Path(__file__).resolve().parents[1]
CONSULTATIONS_DIR = Path("/home/user/taey-ed/consultations")


def _read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def compile_prompt(platform: str, screen_context: dict) -> str:
    # TODO Phase C7: replace this scaffold prompt with the full universal-layer
    # compiler, screen-context shaping, and provisional-knowledge JIT injection.
    universal = _read_text(CONSULTATIONS_DIR / "UNIVERSAL_LAYER_v1.md")
    knowledge = load_knowledge(platform)
    provisional = load_provisional(platform)
    sections = [
        "You are the spark_v2 behavior-tree generator scaffold.",
        "This prompt is assembled in Phase B only to keep the worker contract bootable.",
        "TODO Phase C7: compile the production prompt from canonical sections.",
        "",
        "=== UNIVERSAL LAYER ===",
        universal,
        "",
        "=== KNOWLEDGE JSON ===",
        json.dumps(knowledge, indent=2),
        "",
        "=== PROVISIONAL KNOWLEDGE JSON ===",
        json.dumps(provisional, indent=2) if provisional is not None else "null",
        "",
        "=== SCREEN CONTEXT ===",
        json.dumps(screen_context, indent=2),
    ]
    return "\n".join(sections)
