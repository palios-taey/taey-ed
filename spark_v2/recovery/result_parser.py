"""Validate harvested recovery results and merge them into provisional knowledge."""

from __future__ import annotations

import copy
import json
import time
from pathlib import Path

from spark_v2.tasks.knowledge_loader import load_writable_provisional, merge_provisional_to_global
from spark_v2.utils.atomic_write import atomic_write_json

RECOVERY_DIR = Path("/tmp/taey-ed-recovery")
ALLOWED_CLASSIFICATIONS = {
    "new_screen_variant",
    "stale_platform_knowledge",
    "widget_mechanic_out_of_scope",
    "extraction_mismatch",
    "transient_state_or_bot_interception",
}


def _request_dir(request_id: str) -> Path:
    return RECOVERY_DIR / request_id


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def extract_trailing_json_object(text: str) -> dict:
    raw = str(text or "").strip()
    end = raw.rfind("}")
    if end < 0:
        raise ValueError("no trailing JSON object found")
    depth = 0
    in_string = False
    escape = False
    start = -1
    for index in range(end, -1, -1):
        char = raw[index]
        if escape:
            escape = False
            continue
        if in_string:
            if char == "\\":
                escape = True
            elif char == "\"":
                in_string = False
            continue
        if char == "\"":
            in_string = True
        elif char == "}":
            depth += 1
        elif char == "{":
            depth -= 1
            if depth == 0:
                start = index
                break
    if start < 0:
        raise ValueError("unbalanced trailing JSON object")
    parsed = json.loads(raw[start : end + 1])
    if not isinstance(parsed, dict):
        raise ValueError("trailing JSON payload must be an object")
    return parsed


def _provenance_errors(value, field_path: str) -> list[str]:
    if value is None:
        return []
    if isinstance(value, dict):
        provenance = value.get("provenance")
        if not isinstance(provenance, dict):
            return [f"{field_path}.provenance missing or not an object"]
        return []
    if isinstance(value, list):
        errors: list[str] = []
        for index, item in enumerate(value):
            if isinstance(item, dict):
                provenance = item.get("provenance")
                if not isinstance(provenance, dict):
                    errors.append(f"{field_path}[{index}].provenance missing or not an object")
        return errors
    return []


def _validate_payload(payload: dict, platform: str) -> list[str]:
    errors: list[str] = []
    if not str(payload.get("amendment_rationale") or "").strip():
        errors.append("amendment_rationale must be present and non-empty")
    if payload.get("schema_version") != "v2":
        errors.append("schema_version must equal v2")
    platform_block = payload.get("platform")
    if not isinstance(platform_block, dict) or platform_block.get("name") != platform:
        errors.append(f"platform.name must equal {platform}")
    classification = str(payload.get("recovery_classification") or "")
    if classification not in ALLOWED_CLASSIFICATIONS:
        errors.append("recovery_classification invalid")
    amendments = payload.get("amendments")
    if not isinstance(amendments, dict):
        errors.append("amendments must be an object")
        return errors
    deprecated = amendments.get("deprecated_canonical_paths") or []
    if classification == "stale_platform_knowledge" and not deprecated:
        errors.append("deprecated_canonical_paths required for stale_platform_knowledge")
    if classification == "transient_state_or_bot_interception" and amendments.get("screen_patterns"):
        errors.append("screen_patterns must be empty for transient_state_or_bot_interception")
    for section_name, section_value in amendments.items():
        if section_name == "deprecated_canonical_paths" or not section_value:
            continue
        errors.extend(_provenance_errors(section_value, f"amendments.{section_name}"))
    return errors


def _write_validated(request_id: str, payload: dict) -> None:
    atomic_write_json(_request_dir(request_id) / "validated.json", payload)


def validate_and_merge_recovery_result(result: dict, platform: str, request_id: str) -> tuple[bool, list[str]]:
    errors = _validate_payload(result, platform)
    if errors:
        _write_validated(request_id, {"success": False, "errors": errors})
        return False, errors

    provisional = load_writable_provisional(platform)
    entry = copy.deepcopy(result)
    entry["entry_id"] = f"{request_id}:0"
    entry["request_id"] = request_id
    entry["created_at"] = _now()
    entry["failed_validation_at"] = None
    provisional.setdefault("_recovery_entries", []).append(entry)

    extraction_hints = result.get("extraction_hints")
    if extraction_hints:
        provisional.setdefault("_extraction_hints", {})[entry["entry_id"]] = extraction_hints
    provisional.setdefault("_meta", {})["last_updated_at"] = entry["created_at"]
    merge_provisional_to_global(platform, provisional)

    metadata_path = _request_dir(request_id) / "metadata.json"
    metadata = {}
    if metadata_path.exists():
        try:
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        except Exception:
            metadata = {}
    metadata["status"] = "complete"
    metadata["validated_at"] = entry["created_at"]
    atomic_write_json(metadata_path, metadata)
    _write_validated(request_id, {"success": True, "errors": []})
    return True, []
