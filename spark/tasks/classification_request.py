"""Async screen-classification queue helpers.

Queues classification work for the background worker so /next_action never
blocks on a synchronous LLM subprocess in the request hot path.
"""

from __future__ import annotations

import base64
import json
import logging
import time
from pathlib import Path

from spark.tasks.atomic_write import atomic_write_json
from spark.tasks.paths import is_valid_png_b64

logger = logging.getLogger(__name__)

CLASSIFY_DIR = Path("/tmp/taey-ed-classify")
PENDING_TTL_SECONDS = 600


def _job_dir(platform: str, skel_hash: str) -> Path:
    return CLASSIFY_DIR / platform / f"classify_{skel_hash}"


def _pending_job_is_fresh(meta: dict) -> bool:
    if meta.get("status") != "pending":
        return False
    ts = float(meta.get("updated_at_epoch") or meta.get("created_at_epoch") or 0)
    if ts <= 0:
        return True
    return (time.time() - ts) <= PENDING_TTL_SECONDS


def _read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def request_classification(
    *,
    platform: str,
    tree: dict,
    screenshot_b64: str,
    skel_hash: str,
    session_id: str,
) -> dict:
    """Idempotently queue or read a classification job for this screen hash."""
    CLASSIFY_DIR.mkdir(parents=True, exist_ok=True)
    job_dir = _job_dir(platform, skel_hash)
    response_path = job_dir / "response.json"
    meta_path = job_dir / "metadata.json"

    if response_path.exists():
        try:
            response = _read_json(response_path)
            return {
                "classification_id": job_dir.name,
                "status": "complete",
                **response,
            }
        except Exception as e:
            logger.warning(
                "classification_request: unreadable response for %s/%s: %s",
                platform,
                skel_hash[:12],
                e,
            )

    existing_meta = {}
    if meta_path.exists():
        try:
            existing_meta = _read_json(meta_path)
            if _pending_job_is_fresh(existing_meta):
                existing_meta["updated_at_epoch"] = time.time()
                atomic_write_json(meta_path, existing_meta)
                return {
                    "classification_id": job_dir.name,
                    "status": "pending",
                    "message": f"Waiting on classification {job_dir.name}",
                }
        except Exception as e:
            logger.warning(
                "classification_request: unreadable metadata for %s/%s: %s",
                platform,
                skel_hash[:12],
                e,
            )

    job_dir.mkdir(parents=True, exist_ok=True)
    atomic_write_json(job_dir / "tree.json", tree)

    if screenshot_b64 and is_valid_png_b64(screenshot_b64):
        (job_dir / "screenshot.png").write_bytes(base64.b64decode(screenshot_b64))
    elif screenshot_b64:
        logger.error(
            "classification_request: rejected screenshot for %s/%s: invalid PNG payload (len=%s)",
            platform,
            skel_hash[:12],
            len(screenshot_b64),
        )

    now = time.time()
    metadata = {
        "task_type": "classification",
        "classification_id": job_dir.name,
        "platform": platform,
        "session_id": session_id,
        "skeleton_hash": skel_hash,
        "status": "pending",
        "created_at_epoch": existing_meta.get("created_at_epoch", now),
        "updated_at_epoch": now,
    }
    atomic_write_json(meta_path, metadata)

    logger.info(
        "classification_request: queued %s for %s/%s",
        job_dir.name,
        platform,
        skel_hash[:12],
    )
    return {
        "classification_id": job_dir.name,
        "status": "pending",
        "message": f"Queued classification {job_dir.name}",
    }
