"""Screen-type utility helpers for spark_v2."""

from __future__ import annotations


def get_master_category(screen_type: str | None) -> str:
    # TODO Phase C6: centralize category normalization for the new cache model.
    if not screen_type:
        return "UNKNOWN"
    return screen_type.split(":", 1)[0].split("_", 1)[0].upper()


def is_deterministic(screen_type: str | None) -> bool:
    # TODO Phase F: align deterministic/procedural cache classes with promotion logic.
    return get_master_category(screen_type) in {"NAVIGATION", "VIDEO", "ARTICLE", "TRANSITION"}
