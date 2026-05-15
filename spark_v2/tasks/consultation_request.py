"""Consultation request storage for spark_v2."""

from __future__ import annotations

import base64
import json
import time
import uuid
from pathlib import Path

from spark_v2.utils.atomic_write import atomic_write_json

CONSULT_DIR = Path("/tmp/taey-ed-consult-v2")


def _make_consultation_id() -> str:
    return f"consult_{int(time.time())}_{uuid.uuid4().hex[:8]}"


def request_consultation(
    platform: str,
    tree: dict,
    screenshot_b64: str | None = None,
    metadata: dict | None = None,
) -> dict:
    # TODO Phase D: align request persistence with discovery and recovery loops.
    consultation_id = _make_consultation_id()
    consult_dir = CONSULT_DIR / consultation_id
    consult_dir.mkdir(parents=True, exist_ok=True)
    atomic_write_json(consult_dir / "tree.json", tree)
    if screenshot_b64:
        (consult_dir / "screenshot.png").write_bytes(base64.b64decode(screenshot_b64))
    payload = {
        "consultation_id": consultation_id,
        "platform": platform,
        "status": "pending",
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    if metadata:
        payload.update(metadata)
    atomic_write_json(consult_dir / "metadata.json", payload)
    return {"consultation_id": consultation_id, "status": "pending"}


def poll_consultation(consultation_id: str) -> dict:
    # TODO Phase C2: preserve active consultation polling semantics precisely.
    consult_dir = CONSULT_DIR / consultation_id
    meta_path = consult_dir / "metadata.json"
    response_path = consult_dir / "response.json"
    if not meta_path.exists():
        return {"status": "not_found", "consultation_id": consultation_id}
    metadata = json.loads(meta_path.read_text())
    response = json.loads(response_path.read_text()) if response_path.exists() else None
    return {
        "status": metadata.get("status", "pending"),
        "consultation_id": consultation_id,
        "metadata": metadata,
        "response": response,
    }
