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


def _state_evidence(source: str, **extra) -> dict:
    return {"source": f"classification_request.{source}", **extra}


def _state_repo():
    from spark.state_repo import get_state_repo
    return get_state_repo()


def _mirror_classification_job(
    *,
    platform: str,
    skel_hash: str,
    classification_id: str,
    status: str,
    source: str,
    result: dict | None = None,
) -> None:
    try:
        _state_repo().record_classification_job(
            platform=platform,
            skel_hash=skel_hash,
            classification_id=classification_id,
            status=status,
            result=result,
            actor="api",
            evidence=_state_evidence(source),
        )
    except Exception:
        logger.exception("state-store dual-write failed: classification_request.%s", source)


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
            # A FAILED / UNKNOWN classification must NOT be cached as a permanent
            # 'complete' result. RCA 2026-06-15 (recurring — dropdown then
            # transition): the LLM classifier returning nothing once
            # ({"success": false, "screen_type": "UNKNOWN"}) was served forever,
            # trapping the screen as UNKNOWN -> worker gets the generic guide ->
            # freelance. Treat a failed cached result as STALE and re-queue so the
            # next poll re-classifies (tree/screenshot may be better hydrated, or
            # the transient LLM hiccup clears). Bounded by a small retry cap so a
            # genuinely-unclassifiable screen still settles to UNKNOWN (-> Step 5D
            # worker / escalation) instead of re-running the LLM every poll.
            _ok = response.get("success", True) and \
                str(response.get("screen_type") or "").strip().upper() != "UNKNOWN"
            _retries = 0
            if meta_path.exists():
                try:
                    _retries = int(_read_json(meta_path).get("failed_classify_retries", 0))
                except Exception:
                    _retries = 0
            if _ok or _retries >= 3:
                return {
                    "classification_id": job_dir.name,
                    "status": "complete",
                    **response,
                }
            logger.info(
                "classification_request: cached result for %s/%s is failed/UNKNOWN "
                "(retry %d/3) — re-queueing instead of serving stale UNKNOWN",
                platform, skel_hash[:12], _retries + 1,
            )
            try:
                response_path.unlink()
            except Exception:
                pass
            existing_meta = {}
            if meta_path.exists():
                try:
                    existing_meta = _read_json(meta_path)
                except Exception:
                    existing_meta = {}
            existing_meta["failed_classify_retries"] = _retries + 1
            existing_meta["status"] = "stale_requeue"
            try:
                atomic_write_json(meta_path, existing_meta)
                _mirror_classification_job(
                    platform=platform,
                    skel_hash=skel_hash,
                    classification_id=job_dir.name,
                    status="stale_requeue",
                    source="stale_requeue",
                    result=response,
                )
            except Exception:
                logger.exception("classification_request: failed to bump retry meta")
            # fall through to re-queue a fresh pending job below
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
                _mirror_classification_job(
                    platform=platform,
                    skel_hash=skel_hash,
                    classification_id=job_dir.name,
                    status="pending",
                    source="pending_touch",
                )
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
        # carry the failed-classification retry counter across the re-queue so the
        # cap (3) actually bounds re-runs (the fresh dict would otherwise reset it)
        "failed_classify_retries": existing_meta.get("failed_classify_retries", 0),
    }
    atomic_write_json(meta_path, metadata)
    _mirror_classification_job(
        platform=platform,
        skel_hash=skel_hash,
        classification_id=job_dir.name,
        status="pending",
        source="request_classification",
    )

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
