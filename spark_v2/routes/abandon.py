"""Consultation abandonment route for Mac stop/close flows."""

from __future__ import annotations

import json
import time
from pathlib import Path

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from spark_v2.utils.atomic_write import atomic_write_json

router = APIRouter()
CONSULT_DIR = Path("/tmp/taey-ed-consult-v2")


@router.post("/api/v1/abandon_consultation/{consultation_id}")
async def abandon_consultation(consultation_id: str) -> JSONResponse:
    consult_dir = CONSULT_DIR / consultation_id
    consult_dir.mkdir(parents=True, exist_ok=True)

    response = {
        "tree": {"type": "action", "action": "wait", "params": {"seconds": 5.0}},
        "screen_type": "UNKNOWN",
        "expected_next": [],
        "extract": None,
        "_worker_fallback": True,
        "_worker_failure_reason": "abandoned_by_client",
    }
    atomic_write_json(consult_dir / "response.json", response)

    meta_path = consult_dir / "metadata.json"
    metadata = {
        "consultation_id": consultation_id,
        "status": "abandoned",
        "abandoned_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    if meta_path.exists():
        try:
            existing = json.loads(meta_path.read_text(encoding="utf-8"))
            if isinstance(existing, dict):
                metadata.update(existing)
        except Exception:
            pass
    metadata["status"] = "abandoned"
    metadata["abandoned_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    atomic_write_json(meta_path, metadata)

    return JSONResponse(
        status_code=200,
        content={
            "success": True,
            "consultation_id": consultation_id,
            "abandoned": True,
        },
    )
