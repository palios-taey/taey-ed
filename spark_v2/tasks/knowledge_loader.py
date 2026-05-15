"""Knowledge loading helpers for spark_v2."""

from __future__ import annotations

import json
from pathlib import Path

from spark_v2.utils.atomic_write import atomic_write_json

PLATFORMS_DIR = Path(__file__).resolve().parents[1] / "platforms"


def _platform_dir(platform: str) -> Path:
    return PLATFORMS_DIR / platform


def load_knowledge(platform: str) -> dict:
    # TODO Phase D: extend loader with schema validation and discovery bootstrap.
    path = _platform_dir(platform) / "knowledge.json"
    if not path.exists():
        raise FileNotFoundError(f"knowledge.json missing for platform {platform!r}")
    return json.loads(path.read_text())


def load_provisional(platform: str) -> dict | None:
    # TODO Phase D: wire provisional discovery output into the knowledge gate.
    path = _platform_dir(platform) / "provisional_knowledge.json"
    if not path.exists():
        return None
    return json.loads(path.read_text())


def merge_provisional_to_global(platform: str, provisional_data: dict) -> dict:
    # TODO Phase E: enforce Tier 2.5 promotion rules and provenance validation.
    platform_dir = _platform_dir(platform)
    platform_dir.mkdir(parents=True, exist_ok=True)
    path = platform_dir / "provisional_knowledge.json"
    atomic_write_json(path, provisional_data)
    return provisional_data
