"""
Platform knowledge loader — reads knowledge.json and learned/*.json files.
Provides JIT context assembly for build_bt_from_tree().
"""

import json
import logging
import os
import re
import tempfile
import time
from pathlib import Path
from typing import Optional

from spark.tasks.atomic_write import atomic_write_json
from spark.tasks.screen_type_util import get_master_category

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


def _render_operational_notes(notes: list) -> list:
    """Render a list of operational_note dicts to markdown lines."""
    lines = []
    for n in notes:
        disc = n.get("discovered_at", "")
        by = n.get("by", "")
        note = n.get("note", "")
        template = n.get("bt_template_hint", "")
        disambig = n.get("disambiguator", "")
        handler_req = n.get("handler_required", "")
        prior = n.get("prior_workaround", "")
        verified = n.get("verified_count", 0)
        rule = n.get("rule", "")

        header = f"- *(discovered {disc} by {by}, verified×{verified})*"
        lines.append(header)
        if rule:
            lines.append(f"  **Rule:** {rule}")
        if note:
            lines.append(f"  **Note:** {note}")
        if template:
            lines.append(f"  **BT template hint:** {template}")
        if disambig:
            lines.append(f"  **Disambiguator:** {disambig}")
        if handler_req:
            lines.append(f"  **Requires handler:** {handler_req}")
        if prior:
            lines.append(f"  **Prior workaround:** {prior}")
    return lines


def _match_subtype_for_variant(screen_type: str, master_type: str, subtypes: list) -> dict | None:
    """Pick the subtype whose name matches the variant suffix.
    EXERCISE_DROPDOWN -> "DROPDOWN" -> match subtype name "dropdown".
    Returns None when no clean match found (caller decides whether to fall back).
    """
    if screen_type == master_type:
        return None
    prefix = f"{master_type}_"
    suffix = screen_type[len(prefix):] if screen_type.startswith(prefix) else screen_type
    # Normalize: strip non-alphanumerics, lowercase
    suffix_norm = re.sub(r"[^a-z0-9]+", "", suffix.lower()) if suffix else ""
    if not suffix_norm:
        return None
    for s in subtypes:
        n_norm = re.sub(r"[^a-z0-9]+", "", str(s.get("name", "")).lower())
        if n_norm and (n_norm == suffix_norm or suffix_norm in n_norm or n_norm in suffix_norm):
            return s
    return None


def get_operational_notes_for_screen(knowledge: dict, screen_type: str) -> str:
    """Return markdown-formatted operational notes following Jesse's 3-tier tree:

      1. Platform-level (always)        — knowledge['global']['operational_notes'] (if present)
      2. Category-level (always)        — screen_types.{master}.operational_notes
      3. Sub-category-level (matched)   — screen_types.{master}.subtypes.{variant_match}.operational_notes

    Sibling subtypes are NOT included. Push rules to the lowest applicable level
    to avoid noise. Returns empty string if nothing relevant exists.
    """
    screen_types_map = knowledge.get("screen_types", {})

    # Resolve master category (EXERCISE_DROPDOWN -> EXERCISE)
    try:
        master = get_master_category(screen_type) or screen_type
    except Exception:
        master = screen_type

    sections = []

    # Tier 1: platform-level operational_notes (always included if present)
    platform_notes = knowledge.get("global", {}).get("operational_notes") or []
    if platform_notes:
        lines = ["### platform (always applies)"] + _render_operational_notes(platform_notes)
        sections.append("\n".join(lines))

    master_info = screen_types_map.get(master, {})

    # Tier 2: category-level (master screen type's top-level operational_notes)
    category_notes = master_info.get("operational_notes") or []
    if category_notes:
        lines = [f"### {master} (category-level)"] + _render_operational_notes(category_notes)
        sections.append("\n".join(lines))

    # Tier 3: matched subtype (only the variant's specific subtype, NOT siblings)
    subtypes = master_info.get("subtypes", [])
    matched_subtype = _match_subtype_for_variant(screen_type, master, subtypes)
    if matched_subtype:
        sub_notes = matched_subtype.get("operational_notes") or []
        if sub_notes:
            name = matched_subtype.get("name", "unknown")
            lines = [f"### {master}.{name} (subtype-level — matched on variant {screen_type})"]
            lines += _render_operational_notes(sub_notes)
            sections.append("\n".join(lines))
    elif screen_type == master:
        # When the consult arrived with the master type itself (no subtype suffix),
        # include ALL subtype notes — the worker doesn't know yet which variant applies.
        for s in subtypes:
            sub_notes = s.get("operational_notes") or []
            if not sub_notes:
                continue
            name = s.get("name", "unknown")
            lines = [f"### {master}.{name} (subtype-level — master-only consult, all included)"]
            lines += _render_operational_notes(sub_notes)
            sections.append("\n".join(lines))

    if not sections:
        return ""
    return "## Operational notes (lessons from prior consultations)\n\n" + "\n\n".join(sections)


def _normalize_subtype_name(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", str(name).lower())


def _variant_subtype_key(screen_type: str, master_type: str) -> str:
    if screen_type == master_type:
        return ""
    prefix = f"{master_type}_"
    suffix = screen_type[len(prefix):] if screen_type.startswith(prefix) else screen_type
    return _normalize_subtype_name(suffix)


def _parse_bt_template_hint_json(bt_template_hint: str) -> dict | None:
    if not isinstance(bt_template_hint, str):
        return None
    matches = re.findall(r"```json\s*(\{.*?\})\s*```", bt_template_hint, flags=re.DOTALL)
    candidates = list(reversed(matches))
    stripped = bt_template_hint.strip()
    if stripped.startswith("{") and stripped.endswith("}"):
        candidates.append(stripped)
    for candidate in candidates:
        try:
            parsed = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            return parsed
    return None


def _coerce_bt_template(template: dict | None) -> dict | None:
    if not isinstance(template, dict):
        return None
    if isinstance(template.get("tree"), dict):
        return template
    if isinstance(template.get("behavior_tree"), dict):
        return {
            "tree": template["behavior_tree"],
            "extract": template.get("extract"),
            "expected_next": template.get("expected_next", []),
        }
    if template.get("type") in {"action", "sequence", "selector", "parallel", "conditional"}:
        return {
            "tree": template,
            "extract": None,
            "expected_next": [],
        }
    return None


def _iter_verified_bt_templates(knowledge: dict, screen_type: str, min_verified: int = 1) -> list[dict]:
    master_type = get_master_category(screen_type) or screen_type
    screen_info = knowledge.get("screen_types", {}).get(master_type, {})
    subtypes = screen_info.get("subtypes", [])
    subtype_key = _variant_subtype_key(screen_type, master_type)

    ordered_subtypes = []
    if subtype_key:
        exact = [
            subtype for subtype in subtypes
            if _normalize_subtype_name(subtype.get("name", "")) == subtype_key
        ]
        ordered_subtypes.extend(exact)
        ordered_subtypes.extend(
            subtype for subtype in subtypes
            if _normalize_subtype_name(subtype.get("name", "")) != subtype_key
        )
    else:
        ordered_subtypes = list(subtypes)

    templates = []
    for subtype in ordered_subtypes:
        subtype_name = subtype.get("name", "")
        for note in subtype.get("operational_notes") or []:
            try:
                verified_count = int(note.get("verified_count", 0) or 0)
            except (TypeError, ValueError):
                verified_count = 0
            if verified_count < min_verified:
                continue

            parsed = None
            if isinstance(note.get("bt_template"), dict):
                parsed = _coerce_bt_template(note.get("bt_template"))
            if parsed is None:
                parsed = _coerce_bt_template(
                    _parse_bt_template_hint_json(str(note.get("bt_template_hint") or ""))
                )
            if parsed is None:
                continue

            templates.append({
                "template": parsed,
                "source": {
                    "kind": "operational_note",
                    "master_screen_type": master_type,
                    "screen_type": screen_type,
                    "subtype_name": subtype_name,
                    "discovered_at": note.get("discovered_at", ""),
                    "note": note.get("note", ""),
                    "verified_count": verified_count,
                },
            })
    return templates


def get_verified_bt_template(knowledge: dict, screen_type: str, min_verified: int = 1) -> dict | None:
    """Return the first verified operational-note BT template for a screen variant."""
    templates = _iter_verified_bt_templates(knowledge, screen_type, min_verified=min_verified)
    if not templates:
        return None
    return templates[0]["template"]


def get_verified_bt_template_entry(knowledge: dict, screen_type: str, min_verified: int = 1) -> dict | None:
    """Return template + source metadata for the first verified operational note."""
    templates = _iter_verified_bt_templates(knowledge, screen_type, min_verified=min_verified)
    return templates[0] if templates else None


def increment_operational_note_verified_count(platform: str, source: dict, increment: int = 1) -> bool:
    """Increment verified_count for a specific operational note source."""
    if not isinstance(source, dict) or source.get("kind") != "operational_note":
        return False

    knowledge_path = _platforms_dir() / platform / "knowledge.json"
    if not knowledge_path.exists():
        return False

    try:
        knowledge = json.loads(knowledge_path.read_text())
        master_type = source.get("master_screen_type") or source.get("screen_type") or ""
        screen = knowledge.get("screen_types", {}).get(master_type, {})
        subtypes = screen.get("subtypes", [])
        subtype_name = source.get("subtype_name", "")
        target_note = source.get("note", "")
        discovered_at = source.get("discovered_at", "")

        for subtype in subtypes:
            if subtype.get("name") != subtype_name:
                continue
            notes = subtype.get("operational_notes") or []
            for note in notes:
                if note.get("note") != target_note:
                    continue
                if discovered_at and note.get("discovered_at") != discovered_at:
                    continue
                note["verified_count"] = int(note.get("verified_count", 0) or 0) + increment
                note["last_verified_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
                atomic_write_json(knowledge_path, knowledge)
                cache_key = str(knowledge_path)
                _knowledge_cache.pop(cache_key, None)
                _knowledge_cache_mtime.pop(cache_key, None)
                logger.info(
                    f"increment_operational_note_verified_count: {platform}/{master_type}/{subtype_name} "
                    f"-> {note['verified_count']}"
                )
                return True
    except Exception as e:
        logger.warning(f"increment_operational_note_verified_count failed: {e}")
    return False


def record_operational_note(
    platform: str,
    screen_type: str,
    subtype_name: str,
    note: str,
    *,
    by: str = "claude-primary",
    bt_template_hint: str | None = None,
    disambiguator: str | None = None,
    handler_required: str | None = None,
    prior_workaround: str | None = None,
    verified_count: int = 1,
) -> bool:
    """Append an operational note to a screen subtype's `operational_notes` array.

    Used after a claude-primary consultation successfully solves a tricky widget,
    so the lesson persists for next-Claude. Idempotent on identical `note` text
    (increments verified_count instead of duplicating).

    Returns True if the knowledge.json was modified, False on failure or no-op.
    """
    knowledge_path = _platforms_dir() / platform / "knowledge.json"
    if not knowledge_path.exists():
        logger.error(f"record_operational_note: no knowledge.json for {platform}")
        return False

    try:
        knowledge = json.loads(knowledge_path.read_text())
        screen = knowledge.get("screen_types", {}).get(screen_type)
        if not screen:
            logger.error(
                f"record_operational_note: screen_type {screen_type} not in {platform}"
            )
            return False
        subtypes = screen.setdefault("subtypes", [])
        subtype = next((s for s in subtypes if s.get("name") == subtype_name), None)
        if subtype is None:
            logger.error(
                f"record_operational_note: subtype {subtype_name} not in "
                f"{platform}/{screen_type}"
            )
            return False

        notes = subtype.setdefault("operational_notes", [])
        existing = next((n for n in notes if n.get("note") == note), None)
        if existing:
            existing["verified_count"] = existing.get("verified_count", 1) + 1
            existing["last_verified_at"] = time.strftime(
                "%Y-%m-%dT%H:%M:%SZ", time.gmtime()
            )
        else:
            entry = {
                "discovered_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "by": by,
                "note": note,
                "verified_count": verified_count,
            }
            if bt_template_hint:
                entry["bt_template_hint"] = bt_template_hint
            if disambiguator:
                entry["disambiguator"] = disambiguator
            if handler_required:
                entry["handler_required"] = handler_required
            if prior_workaround:
                entry["prior_workaround"] = prior_workaround
            notes.append(entry)


        # Atomic write
        fd, tmp_path = tempfile.mkstemp(
            dir=str(knowledge_path.parent), suffix=".json.tmp"
        )
        try:
            with os.fdopen(fd, "w") as f:
                json.dump(knowledge, f, indent=2)
            os.replace(tmp_path, str(knowledge_path))
        except Exception:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise

        # Invalidate cache so next load_knowledge sees the update
        cache_key = str(knowledge_path)
        _knowledge_cache.pop(cache_key, None)
        _knowledge_cache_mtime.pop(cache_key, None)

        action = "incremented" if existing else "added"
        logger.info(
            f"record_operational_note: {action} note for "
            f"{platform}/{screen_type}/{subtype_name}"
        )
        return True

    except Exception as e:
        logger.error(f"record_operational_note: failed: {e}")
        return False


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
