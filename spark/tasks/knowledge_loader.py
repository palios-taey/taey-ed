"""
Platform knowledge loader.

Post-scr1 slash shape:
  - load platform identity / quirks from knowledge.json
  - merge universal rules from spark/platforms/_universal.json
  - expose knowledge versioning and learned-observation storage

Learning no longer self-rewrites knowledge.json. Supervisor YAML edits are the
only path for screen-program changes.
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Optional

from spark.tasks.atomic_write import atomic_write_json

logger = logging.getLogger("taey-ed")

_knowledge_cache: dict[str, dict] = {}
_knowledge_cache_mtime: dict[str, float] = {}


def _platforms_dir() -> Path:
    candidates = [
        Path(__file__).parent.parent / "platforms",
        Path("spark/platforms"),
    ]
    for path in candidates:
        if path.exists():
            return path
    return candidates[0]


def load_knowledge(platform: str) -> dict:
    knowledge_path = _platforms_dir() / platform / "knowledge.json"
    if not knowledge_path.exists():
        return {}

    try:
        current_mtime = knowledge_path.stat().st_mtime
        cache_key = str(knowledge_path)
        if (
            cache_key in _knowledge_cache
            and _knowledge_cache_mtime.get(cache_key) == current_mtime
        ):
            return _knowledge_cache[cache_key]

        knowledge = json.loads(knowledge_path.read_text(encoding="utf-8"))
        # screen_types removed 2026-06-12 (scr1-knowledge-shrink): screen knowledge
        # now lives in screen_types/*.yaml; knowledge.json is identity+quirks+guide.
        required_keys = ["platform", "schema_version", "global"]
        missing = [k for k in required_keys if k not in knowledge]
        if missing:
            logger.error(
                f"knowledge.json for {platform} missing required keys: {missing}. Falling back to empty knowledge."
            )
            return {}

        try:
            universal_path = _platforms_dir() / "_universal.json"
            if universal_path.exists():
                universal = json.loads(universal_path.read_text(encoding="utf-8"))
                universal_notes = universal.get("operational_notes") or []
                if universal_notes:
                    knowledge.setdefault("global", {}).setdefault("operational_notes", [])
                    knowledge["global"]["operational_notes"] = (
                        list(universal_notes) + knowledge["global"]["operational_notes"]
                    )
        except Exception:
            logger.exception("Failed to merge _universal.json (non-fatal)")

        _knowledge_cache[cache_key] = knowledge
        _knowledge_cache_mtime[cache_key] = current_mtime
        return knowledge
    except (json.JSONDecodeError, OSError) as e:
        logger.error(f"Failed to load knowledge.json for {platform}: {e}")
        return {}


# load_learned() and get_quirks_for_screen() removed 2026-07-09
# (cleanup-dead-apis): zero callers — orphaned by the scr1 knowledge-shrink
# (screen knowledge moved to screen_types/*.yaml). The WRITE side
# (save_learned_observation) stays live until its replacement lands
# (taey-ed-state-context p2 events/qa_captures); learned/*.json migrate-or-drop
# is decided at p2-importer. REQUIREMENTS.md O1.


def get_knowledge_version(platform: str) -> Optional[str]:
    knowledge_path = _platforms_dir() / platform / "knowledge.json"
    if not knowledge_path.exists():
        return None
    try:
        knowledge = load_knowledge(platform)
        last_researched = knowledge.get("last_researched", "")
        mtime = str(knowledge_path.stat().st_mtime)
        return f"{last_researched}:{mtime}"
    except Exception:
        return None


def save_learned_observation(platform: str, screen_type: str, observation: dict):
    learned_dir = _platforms_dir() / platform / "learned"
    learned_dir.mkdir(parents=True, exist_ok=True)
    learned_path = learned_dir / f"{screen_type}.json"

    try:
        if learned_path.exists():
            current = json.loads(learned_path.read_text(encoding="utf-8"))
        else:
            current = {
                "$schema": "taey-ed-learned-v1",
                "platform": platform,
                "screen_type": screen_type,
                "observations": [],
                "latest_summary": {},
            }

        current["observations"].append(observation)
        if len(current["observations"]) > 20:
            current["observations"] = current["observations"][-20:]

        obs_count = len(current["observations"])
        if obs_count % 5 == 0 or not current.get("latest_summary"):
            current["latest_summary"] = _generate_summary(current["observations"])

        atomic_write_json(learned_path, current)
    except Exception as e:
        logger.error(f"Failed to save learned observation: {e}")


def _generate_summary(observations: list) -> dict:
    successful = [o for o in observations if o.get("bt_success")]
    failed = [o for o in observations if not o.get("bt_success")]

    successful_patterns = []
    submit_variants = set()
    for obs in successful:
        details = obs.get("details", {})
        submit = details.get("submit_button", {})
        if submit and submit.get("text"):
            submit_variants.add(submit["text"])
        strategy = details.get("answer_strategy") or details.get("click_strategies")
        if strategy:
            variant = obs.get("variant", "")
            successful_patterns.append(f"{variant}: {strategy}")

    known_failures = []
    for obs in failed:
        reason = obs.get("failure_reason", "")
        fix = obs.get("fix_applied", "")
        if reason:
            entry = reason
            if fix:
                entry += f" — fixed by: {fix}"
            known_failures.append(entry)

    return {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "total_observations": len(observations),
        "successful_patterns": successful_patterns[-10:],
        "known_failures": known_failures[-5:],
        "submit_button_variants": sorted(submit_variants),
    }
