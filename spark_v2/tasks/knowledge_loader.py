"""Knowledge loading helpers for spark_v2."""

from __future__ import annotations

import copy
import json
import time
from pathlib import Path
from typing import Any

from spark_v2.utils.atomic_write import atomic_write_json

PLATFORMS_DIR = Path(__file__).resolve().parents[1] / "platforms"


def _platform_dir(platform: str) -> Path:
    return PLATFORMS_DIR / platform


def _display_name(platform: str) -> str:
    return " ".join(part.capitalize() for part in platform.split("_")) or platform


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _empty_shell(platform: str) -> dict:
    return {
        "schema_version": "v2",
        "platform": {
            "name": platform,
            "display_name": _display_name(platform),
            "url_pattern": platform,
            "platform_type": "other",
            "framework_hint": None,
            "discovered_at": None,
            "discovery_source": None,
        },
        "global": {
            "completion_indicators": [],
            "advancement_link_patterns": [],
            "video_completion_signal": None,
            "timing_characteristics": None,
        },
        "screen_patterns": {},
        "never_clicks_platform": [],
        "widget_classes": {},
        "cached_bts": {},
        "_meta": {
            "created_at": None,
            "last_updated_at": None,
            "discovery_event_id": None,
            "schema_version": "v2",
            "validating_consults_total": 0,
            "discovery_consults_total": 0,
            "recovery_consults_total": 0,
            "user_assist_events_total": 0,
        },
    }


def _empty_provisional_shell(platform: str) -> dict:
    data = _empty_shell(platform)
    data["_recovery_entries"] = []
    data["_extraction_hints"] = {}
    data["_meta"]["failed_attempts"] = []
    return data


def is_first_touch(platform_data: dict) -> bool:
    if not platform_data:
        return True
    if any(platform_data.get("global", {}).get(key) for key in ("completion_indicators", "advancement_link_patterns")):
        return False
    if platform_data.get("global", {}).get("video_completion_signal") is not None:
        return False
    if platform_data.get("global", {}).get("timing_characteristics") is not None:
        return False
    if platform_data.get("screen_patterns"):
        return False
    if platform_data.get("never_clicks_platform"):
        return False
    if platform_data.get("widget_classes"):
        return False
    if platform_data.get("cached_bts"):
        return False
    return True


def load_knowledge(platform: str) -> dict:
    path = _platform_dir(platform) / "knowledge.json"
    if not path.exists():
        return _empty_shell(platform)
    data = json.loads(path.read_text())
    if is_first_touch(data):
        return data
    return data


def save_knowledge(platform: str, data: dict) -> dict:
    platform_dir = _platform_dir(platform)
    platform_dir.mkdir(parents=True, exist_ok=True)
    atomic_write_json(platform_dir / "knowledge.json", data)
    return data


def load_provisional(platform: str) -> dict | None:
    path = _platform_dir(platform) / "provisional_knowledge.json"
    if not path.exists():
        return None
    data = json.loads(path.read_text())
    return data if data else None


def get_video_completion_signal(platform: str) -> dict | None:
    canonical_signal = (load_knowledge(platform).get("global") or {}).get("video_completion_signal")
    if isinstance(canonical_signal, dict):
        return canonical_signal
    provisional = load_provisional(platform)
    provisional_signal = ((provisional or {}).get("global") or {}).get("video_completion_signal")
    return provisional_signal if isinstance(provisional_signal, dict) else None


def load_writable_provisional(platform: str) -> dict:
    data = copy.deepcopy(load_provisional(platform) or _empty_provisional_shell(platform))
    data.setdefault("_recovery_entries", [])
    data.setdefault("_extraction_hints", {})
    meta = data.setdefault("_meta", {})
    meta.setdefault("failed_attempts", [])
    return data


def iter_provisional_entries(provisional_data: dict | None) -> list[dict]:
    if not isinstance(provisional_data, dict):
        return []
    entries = provisional_data.get("_recovery_entries")
    if not isinstance(entries, list):
        return []
    return [entry for entry in entries if isinstance(entry, dict)]


def active_recovery_entry_ids(provisional_data: dict | None) -> list[str]:
    entry_ids: list[str] = []
    for entry in iter_provisional_entries(provisional_data):
        if entry.get("failed_validation_at"):
            continue
        entry_id = str(entry.get("entry_id") or "").strip()
        if entry_id:
            entry_ids.append(entry_id)
    return entry_ids


def failed_provisional_attempts(provisional_data: dict | None) -> list[dict]:
    if not isinstance(provisional_data, dict):
        return []
    attempts: list[dict] = []
    for entry in iter_provisional_entries(provisional_data):
        if entry.get("failed_validation_at"):
            attempts.append(entry)
    meta_attempts = (provisional_data.get("_meta") or {}).get("failed_attempts")
    if isinstance(meta_attempts, list):
        attempts.extend(item for item in meta_attempts if isinstance(item, dict))
    return attempts


def increment_meta_counter(platform: str, key: str) -> int:
    platform_dir = _platform_dir(platform)
    platform_dir.mkdir(parents=True, exist_ok=True)
    path = platform_dir / "knowledge.json"
    data = load_knowledge(platform) if path.exists() else _empty_shell(platform)
    meta = data.setdefault("_meta", {})
    current = int(meta.get(key) or 0)
    updated = current + 1
    meta[key] = updated
    meta["last_updated_at"] = _now()
    atomic_write_json(path, data)
    return updated


def increment_validating_count(platform: str) -> int:
    return increment_meta_counter(platform, "validating_consults_total")


def merge_provisional_to_global(platform: str, provisional_data: dict) -> dict:
    platform_dir = _platform_dir(platform)
    platform_dir.mkdir(parents=True, exist_ok=True)
    path = platform_dir / "provisional_knowledge.json"
    atomic_write_json(path, provisional_data)
    return provisional_data


def _split_pointer(path: str) -> list[str]:
    return [part for part in str(path or "").strip("/").split("/") if part]


def _read_path(data: Any, path: str) -> Any:
    current = data
    for part in _split_pointer(path):
        if isinstance(current, list):
            index = int(part)
            current = current[index]
        elif isinstance(current, dict):
            current = current[part]
        else:
            raise KeyError(path)
    return current


def _assign_path(data: Any, path: str, value: Any) -> None:
    parts = _split_pointer(path)
    if not parts:
        raise KeyError("empty path")
    current = data
    for index, part in enumerate(parts[:-1]):
        next_part = parts[index + 1]
        if isinstance(current, list):
            current = current[int(part)]
            continue
        if part not in current or current[part] is None:
            current[part] = [] if next_part.isdigit() else {}
        current = current[part]
    leaf = parts[-1]
    if isinstance(current, list):
        target_index = int(leaf)
        while len(current) <= target_index:
            current.append(None)
        current[target_index] = value
    else:
        current[leaf] = value


def _mark_superseded(value: Any, *, event_id: str, timestamp: str) -> Any:
    if isinstance(value, dict):
        updated = copy.deepcopy(value)
        provenance = updated.get("provenance")
        if isinstance(provenance, dict):
            updated["superseded_value"] = copy.deepcopy(
                {key: val for key, val in updated.items() if key != "superseded_value"}
            )
            provenance["epistemic_state"] = "superseded"
            provenance["superseded_by"] = event_id
            provenance["superseded_at"] = timestamp
            updated["provenance"] = provenance
            return updated
    return {
        "superseded_value": copy.deepcopy(value),
        "provenance": {
            "source": "recovery",
            "event_id": event_id,
            "timestamp": timestamp,
            "validated_step2": True,
            "validated_step2_at": timestamp,
            "epistemic_state": "superseded",
            "superseded_by": event_id,
            "superseded_at": timestamp,
        },
    }


def _validate_step2(value: Any, timestamp: str) -> Any:
    if isinstance(value, dict):
        updated = copy.deepcopy(value)
        provenance = updated.get("provenance")
        if isinstance(provenance, dict):
            provenance["validated_step2"] = True
            provenance["validated_step2_at"] = timestamp
            updated["provenance"] = provenance
        return updated
    if isinstance(value, list):
        return [_validate_step2(item, timestamp) for item in value]
    return copy.deepcopy(value)


def _merge_recovery_entry_into_knowledge(knowledge: dict, entry: dict, timestamp: str) -> None:
    amendments = entry.get("amendments") or {}
    for path in amendments.get("deprecated_canonical_paths") or []:
        try:
            current = _read_path(knowledge, path)
        except Exception:
            continue
        _assign_path(
            knowledge,
            path,
            _mark_superseded(current, event_id=str(entry.get("request_id") or entry.get("entry_id") or "recovery"), timestamp=timestamp),
        )

    for section_name, section_value in amendments.items():
        if section_name == "deprecated_canonical_paths" or not section_value:
            continue
        target_key = "global" if section_name == "completion_indicators_global" else section_name
        if target_key == "global" and isinstance(section_value, list):
            knowledge.setdefault("global", {}).setdefault("completion_indicators", [])
            knowledge["global"]["completion_indicators"].extend(
                _validate_step2(section_value, timestamp)
            )
            continue
        current_value = knowledge.setdefault(target_key, {} if isinstance(section_value, dict) else [])
        if isinstance(section_value, dict):
            if not isinstance(current_value, dict):
                knowledge[target_key] = {}
                current_value = knowledge[target_key]
            for child_key, child_value in section_value.items():
                current_value[child_key] = _validate_step2(child_value, timestamp)
        elif isinstance(section_value, list):
            if not isinstance(current_value, list):
                knowledge[target_key] = []
                current_value = knowledge[target_key]
            current_value.extend(_validate_step2(section_value, timestamp))

    extraction_hints = entry.get("extraction_hints") or {}
    if extraction_hints:
        provisional_hints = knowledge.setdefault("_recovery_extraction_hints", {})
        provisional_hints[str(entry.get("entry_id") or entry.get("request_id") or timestamp)] = extraction_hints


def record_failed_recovery_attempt(platform: str, consultation_id: str, timestamp: str | None = None) -> dict | None:
    provisional = load_writable_provisional(platform)
    entries = provisional.get("_recovery_entries", [])
    if not entries:
        return None
    timestamp = timestamp or _now()
    updated_any = False
    failed_entry: dict | None = None
    for entry in entries:
        if not isinstance(entry, dict) or entry.get("failed_validation_at"):
            continue
        entry["failed_validation_at"] = timestamp
        failed_entry = copy.deepcopy(entry)
        updated_any = True
    if not updated_any:
        return None
    meta = provisional.setdefault("_meta", {})
    failed_attempts = meta.setdefault("failed_attempts", [])
    failed_attempts.append(
        {
            "consultation_id": consultation_id,
            "failed_at": timestamp,
            "entries": copy.deepcopy(entries),
        }
    )
    atomic_write_json(_platform_dir(platform) / "provisional_knowledge.json", provisional)
    return failed_entry


def graduate_active_recovery_entries(platform: str, consultation_id: str, timestamp: str | None = None) -> dict:
    platform_dir = _platform_dir(platform)
    knowledge_path = platform_dir / "knowledge.json"
    provisional_path = platform_dir / "provisional_knowledge.json"
    knowledge_before = load_knowledge(platform) if knowledge_path.exists() else _empty_shell(platform)
    provisional_before = load_writable_provisional(platform)
    timestamp = timestamp or _now()

    entries = [entry for entry in iter_provisional_entries(provisional_before) if not entry.get("failed_validation_at")]
    if not entries:
        return {"graduated_entry_ids": [], "consultation_id": consultation_id, "timestamp": timestamp}

    knowledge_after = copy.deepcopy(knowledge_before)
    provisional_after = copy.deepcopy(provisional_before)
    remaining_entries: list[dict] = []

    for entry in iter_provisional_entries(provisional_before):
        if entry.get("failed_validation_at"):
            remaining_entries.append(copy.deepcopy(entry))
            continue
        _merge_recovery_entry_into_knowledge(knowledge_after, entry, timestamp)

    provisional_after["_recovery_entries"] = remaining_entries
    knowledge_after.setdefault("_meta", {})["last_updated_at"] = timestamp
    provisional_after.setdefault("_meta", {})["last_updated_at"] = timestamp

    try:
        atomic_write_json(knowledge_path, knowledge_after)
        atomic_write_json(provisional_path, provisional_after)
    except Exception:
        atomic_write_json(knowledge_path, knowledge_before)
        atomic_write_json(provisional_path, provisional_before)
        raise

    return {
        "graduated_entry_ids": [str(entry.get("entry_id") or "") for entry in entries if str(entry.get("entry_id") or "")],
        "consultation_id": consultation_id,
        "timestamp": timestamp,
    }
