"""Post-promotion invalidation callbacks."""

from __future__ import annotations

from datetime import datetime, timezone

from spark_v2.learning.cache import INVALIDATION_AT


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def on_post_promotion_failure(skeleton_hash: str, platform: str, knowledge: dict) -> None:
    _ = platform
    cached_bts = knowledge.get("cached_bts", {})
    if not isinstance(cached_bts, dict):
        return
    entry = cached_bts.get(skeleton_hash)
    if not isinstance(entry, dict):
        return
    entry["consecutive_failures_post_promotion"] = int(
        entry.get("consecutive_failures_post_promotion", 0)
    ) + 1
    if entry["consecutive_failures_post_promotion"] >= INVALIDATION_AT:
        del cached_bts[skeleton_hash]


def on_post_promotion_success(skeleton_hash: str, platform: str, knowledge: dict) -> None:
    _ = platform
    cached_bts = knowledge.get("cached_bts", {})
    if not isinstance(cached_bts, dict):
        return
    entry = cached_bts.get(skeleton_hash)
    if not isinstance(entry, dict):
        return
    entry["consecutive_failures_post_promotion"] = 0
    entry["last_validated_at"] = _now()
