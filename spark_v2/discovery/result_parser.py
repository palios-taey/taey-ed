"""Validate harvested discovery results and promote them to provisional knowledge."""

from __future__ import annotations

import json
from pathlib import Path

from spark_v2.utils.atomic_write import atomic_write_json

DISCOVERY_DIR = Path("/tmp/taey-ed-discovery")
PLATFORMS_DIR = Path(__file__).resolve().parents[1] / "platforms"


def _request_dir(request_id: str) -> Path:
    return DISCOVERY_DIR / request_id


def _provenance_errors(provenance: dict | None, field_path: str) -> list[str]:
    if not isinstance(provenance, dict):
        return [f"{field_path}.provenance missing or not an object"]
    errors: list[str] = []
    if provenance.get("source") != "discovery":
        errors.append(f"{field_path}.provenance.source must be discovery")
    if provenance.get("validated_step2") is not False:
        errors.append(f"{field_path}.provenance.validated_step2 must be false")
    return errors


def _validate_global(global_data: dict, errors: list[str]) -> None:
    completion = global_data.get("completion_indicators")
    if completion is not None:
        if not isinstance(completion, list):
            errors.append("global.completion_indicators must be a list")
        else:
            for index, item in enumerate(completion):
                if not isinstance(item, dict):
                    errors.append(f"global.completion_indicators[{index}] must be an object")
                    continue
                errors.extend(
                    _provenance_errors(item.get("provenance"), f"global.completion_indicators[{index}]")
                )

    advancement = global_data.get("advancement_link_patterns")
    if advancement is not None:
        if not isinstance(advancement, list):
            errors.append("global.advancement_link_patterns must be a list")
        else:
            for index, item in enumerate(advancement):
                if not isinstance(item, dict):
                    errors.append(f"global.advancement_link_patterns[{index}] must be an object")
                    continue
                errors.extend(
                    _provenance_errors(item.get("provenance"), f"global.advancement_link_patterns[{index}]")
                )

    for key in ("video_completion_signal", "timing_characteristics"):
        value = global_data.get(key)
        if value is None:
            continue
        if not isinstance(value, dict):
            errors.append(f"global.{key} must be an object or null")
            continue
        errors.extend(_provenance_errors(value.get("provenance"), f"global.{key}"))


def _validate_screen_patterns(screen_patterns: dict, errors: list[str]) -> None:
    if not isinstance(screen_patterns, dict):
        errors.append("screen_patterns must be an object")
        return
    for key, value in screen_patterns.items():
        if value is None:
            continue
        if not isinstance(value, dict):
            errors.append(f"screen_patterns.{key} must be an object or null")
            continue
        errors.extend(_provenance_errors(value.get("provenance"), f"screen_patterns.{key}"))


def _validate_never_clicks(entries: list, errors: list[str]) -> None:
    if not isinstance(entries, list):
        errors.append("never_clicks_platform must be a list")
        return
    for index, item in enumerate(entries):
        if not isinstance(item, dict):
            errors.append(f"never_clicks_platform[{index}] must be an object")
            continue
        errors.extend(_provenance_errors(item.get("provenance"), f"never_clicks_platform[{index}]"))


def _write_validated(request_id: str, payload: dict) -> None:
    atomic_write_json(_request_dir(request_id) / "validated.json", payload)


def validate_and_promote_to_provisional(result: dict, platform: str, request_id: str) -> tuple[bool, list[str]]:
    errors: list[str] = []

    if result.get("schema_version") != "v2":
        errors.append("schema_version must equal v2")

    platform_block = result.get("platform")
    if not isinstance(platform_block, dict):
        errors.append("platform must be an object")
    elif platform_block.get("name") != platform:
        errors.append(f"platform.name must equal {platform}")

    global_data = result.get("global")
    if not isinstance(global_data, dict):
        errors.append("global must be an object")
    else:
        _validate_global(global_data, errors)

    screen_patterns = result.get("screen_patterns")
    if screen_patterns is None:
        errors.append("screen_patterns must be present")
    else:
        _validate_screen_patterns(screen_patterns, errors)

    never_clicks = result.get("never_clicks_platform")
    if never_clicks is None:
        errors.append("never_clicks_platform must be present")
    else:
        _validate_never_clicks(never_clicks, errors)

    widget_classes = result.get("widget_classes")
    if widget_classes != {}:
        errors.append("widget_classes must be an empty object during discovery")

    cached_bts = result.get("cached_bts")
    if cached_bts != {}:
        errors.append("cached_bts must be an empty object during discovery")

    if errors:
        _write_validated(request_id, {"success": False, "errors": errors})
        return False, errors

    platform_dir = PLATFORMS_DIR / platform
    platform_dir.mkdir(parents=True, exist_ok=True)
    atomic_write_json(platform_dir / "provisional_knowledge.json", result)

    metadata_path = _request_dir(request_id) / "metadata.json"
    metadata = {}
    if metadata_path.exists():
        try:
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        except Exception:
            metadata = {}
    metadata["status"] = "complete"
    metadata["validated_at"] = result.get("_meta", {}).get("last_updated_at")
    atomic_write_json(metadata_path, metadata)
    _write_validated(request_id, {"success": True, "errors": []})
    return True, []
