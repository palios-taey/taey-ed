"""Poll recovery-loop artifacts written to disk."""

from __future__ import annotations

import json
from pathlib import Path

RECOVERY_DIR = Path("/tmp/taey-ed-recovery")


def _request_dir(request_id: str) -> Path:
    return RECOVERY_DIR / request_id


def _request_timestamp(request_id: str) -> int:
    try:
        return int(request_id.split("_")[-2])
    except Exception:
        return 0


def find_active_recovery_request(platform: str) -> str | None:
    prefix = f"recovery_{platform}_"
    matches: list[str] = []
    if not RECOVERY_DIR.exists():
        return None
    for child in RECOVERY_DIR.iterdir():
        if not child.is_dir() or not child.name.startswith(prefix):
            continue
        metadata_path = child / "metadata.json"
        if not metadata_path.exists():
            continue
        try:
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        except Exception:
            continue
        if metadata.get("status") in {"pending", "harvested"}:
            matches.append(child.name)
    if not matches:
        return None
    matches.sort(key=_request_timestamp, reverse=True)
    return matches[0]


def poll_for_result(request_id: str) -> dict | None:
    result_path = _request_dir(request_id) / "result.json"
    if not result_path.exists():
        return None
    return json.loads(result_path.read_text(encoding="utf-8"))


def get_metadata_status(request_id: str) -> str | None:
    metadata_path = _request_dir(request_id) / "metadata.json"
    if not metadata_path.exists():
        return None
    try:
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    except Exception:
        return None
    status = metadata.get("status")
    return status if isinstance(status, str) else None
