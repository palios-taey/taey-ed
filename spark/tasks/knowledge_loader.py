"""
Platform knowledge loader — reads knowledge.json and learned/*.json files.
Provides JIT context assembly for build_bt_from_tree().
"""

import json
import logging
import os
import tempfile
import time
from pathlib import Path
from typing import Optional

logger = logging.getLogger("taey-ed")

# Cache is per-process. Invalidated on restart or explicit clear.
_knowledge_cache = {}
_knowledge_cache_mtime = {}


def _platforms_dir() -> Path:
    """Return the platforms directory path."""
    candidates = [
        Path(__file__).parent.parent / "platforms",
        Path("spark/platforms"),
    ]
    for p in candidates:
        if p.exists():
            return p
    return candidates[0]


def load_knowledge(platform: str) -> dict:
    """
    Load knowledge.json for a platform. Returns empty dict if missing.
    Cached in memory — checks file mtime to detect changes.
    """
    knowledge_path = _platforms_dir() / platform / "knowledge.json"

    if not knowledge_path.exists():
        return {}

    try:
        current_mtime = knowledge_path.stat().st_mtime
        cache_key = str(knowledge_path)
        if (cache_key in _knowledge_cache and
                _knowledge_cache_mtime.get(cache_key) == current_mtime):
            return _knowledge_cache[cache_key]

        knowledge = json.loads(knowledge_path.read_text())

        # Validate required fields on load
        required_keys = ["platform", "schema_version", "global", "screen_types"]
        missing = [k for k in required_keys if k not in knowledge]
        if missing:
            logger.error(
                f"knowledge.json for {platform} missing required keys: {missing}. "
                f"Falling back to empty knowledge."
            )
            return {}

        _knowledge_cache[cache_key] = knowledge
        _knowledge_cache_mtime[cache_key] = current_mtime
        logger.info(f"Loaded knowledge.json for {platform} (v{knowledge.get('schema_version', '?')})")
        return knowledge

    except (json.JSONDecodeError, OSError) as e:
        logger.error(f"Failed to load knowledge.json for {platform}: {e}")
        return {}


def load_learned(platform: str, screen_type: str) -> dict:
    """
    Load learned observations for a specific screen type.
    Returns empty dict if file missing. No caching (changes frequently).
    """
    learned_path = _platforms_dir() / platform / "learned" / f"{screen_type}.json"

    if not learned_path.exists():
        return {}

    try:
        return json.loads(learned_path.read_text())
    except (json.JSONDecodeError, OSError) as e:
        logger.warning(f"Failed to load learned/{screen_type}.json for {platform}: {e}")
        return {}


def get_handlers_for_screen(knowledge: dict, screen_type: str, tags: list) -> list:
    """
    Return handler names needed for this screen type + detected tags.
    Returns empty list if knowledge has no info (triggers fallback to full docs).
    """
    screen_info = knowledge.get("screen_types", {}).get(screen_type, {})
    handlers_config = screen_info.get("handlers_needed", {})

    if not handlers_config:
        return []

    handlers = set(handlers_config.get("always", []))

    conditional = handlers_config.get("conditional", {})
    for tag in tags:
        if tag in conditional:
            handlers.update(conditional[tag])

    return sorted(handlers)


def get_quirks_for_screen(knowledge: dict, screen_type: str) -> list:
    """Return platform quirks that affect this screen type."""
    all_quirks = knowledge.get("global", {}).get("platform_quirks", [])
    return [q for q in all_quirks if screen_type in q.get("affects", [])]


def get_question_types_for_screen(knowledge: dict, screen_type: str, tags: list) -> list:
    """Return question type names needed for this screen type."""
    screen_info = knowledge.get("screen_types", {}).get(screen_type, {})
    qt_config = screen_info.get("question_types", {})

    if not qt_config:
        return []

    types = set()
    # "always" key for types that always apply
    if "always" in qt_config:
        val = qt_config["always"]
        if isinstance(val, list):
            types.update(val)
        else:
            types.add(val)

    # Tag-driven types
    for tag in tags:
        if tag in qt_config:
            val = qt_config[tag]
            if isinstance(val, list):
                types.update(val)
            else:
                types.add(val)

    return sorted(types)


def get_knowledge_version(platform: str) -> Optional[str]:
    """
    Return a version string for the current knowledge state.
    Used for deterministic BT cache invalidation.
    """
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
    """
    Append an observation to learned/{screen_type}.json.
    Uses atomic write-via-temp-file to prevent corruption.
    """
    learned_dir = _platforms_dir() / platform / "learned"
    learned_dir.mkdir(parents=True, exist_ok=True)
    learned_path = learned_dir / f"{screen_type}.json"

    try:
        # Read current
        if learned_path.exists():
            current = json.loads(learned_path.read_text())
        else:
            current = {
                "$schema": "taey-ed-learned-v1",
                "platform": platform,
                "screen_type": screen_type,
                "observations": [],
                "latest_summary": {},
            }

        # Append observation
        current["observations"].append(observation)

        # Prune to last 20 observations
        if len(current["observations"]) > 20:
            current["observations"] = current["observations"][-20:]

        # Regenerate summary every 5 observations
        obs_count = len(current["observations"])
        if obs_count % 5 == 0 or obs_count == 1:
            current["latest_summary"] = _generate_summary(current["observations"])

        # Atomic write — write to temp, then rename
        fd, tmp_path = tempfile.mkstemp(
            dir=str(learned_dir), suffix=".json.tmp"
        )
        try:
            with os.fdopen(fd, "w") as f:
                json.dump(current, f, indent=2)
            os.replace(tmp_path, str(learned_path))
            logger.info(
                f"Saved learned observation for {platform}/{screen_type} "
                f"(total: {obs_count})"
            )
        except Exception:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise

    except Exception as e:
        logger.error(f"Failed to save learned observation: {e}")


def _generate_summary(observations: list) -> dict:
    """Rebuild latest_summary from observations array."""
    successful = [o for o in observations if o.get("bt_success")]
    failed = [o for o in observations if not o.get("bt_success")]

    # Extract patterns from successful runs
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

    # Extract known failures
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
