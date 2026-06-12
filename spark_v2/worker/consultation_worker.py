"""Consultation worker for spark_v2."""

from __future__ import annotations

import json
import logging
import os
import time
from concurrent.futures import Future, ThreadPoolExecutor
from pathlib import Path

from spark_v2.utils.atomic_write import atomic_write_json
from spark_v2.worker.bt_generator import BTGenerationError, generate_bt

logger = logging.getLogger(__name__)

CONSULT_DIR = Path("/tmp/taey-ed-consult-v2")
POLL_INTERVAL_S = 2.0
MAX_CONCURRENT_JOBS = 3
JOB_TIMEOUT_S = 180.0


def _list_pending_consultations() -> list[str]:
    if not CONSULT_DIR.exists():
        return []
    pending: list[str] = []
    for child in CONSULT_DIR.iterdir():
        if not child.is_dir() or not child.name.startswith("consult_"):
            continue
        meta_path = child / "metadata.json"
        response_path = child / "response.json"
        if not meta_path.exists() or response_path.exists():
            continue
        try:
            metadata = json.loads(meta_path.read_text())
        except Exception:
            continue
        if metadata.get("status") == "pending":
            pending.append(child.name)
    return pending


def _write_user_input_needed_fallback(consultation_id: str, reason: str) -> None:
    consult_dir = CONSULT_DIR / consultation_id
    response = {
        "tree": {"type": "action", "action": "wait", "params": {"seconds": 5.0}},
        "screen_type": "UNKNOWN",
        "expected_next": [],
        "extract": None,
        "_worker_fallback": True,
        "_worker_failure_reason": reason,
    }
    atomic_write_json(consult_dir / "response.json", response)
    meta_path = consult_dir / "metadata.json"
    if meta_path.exists():
        try:
            metadata = json.loads(meta_path.read_text())
            metadata["status"] = "worker_failed"
            metadata["worker_failure_reason"] = reason
            metadata["responded_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
            atomic_write_json(meta_path, metadata)
        except Exception:
            logger.exception("worker fallback metadata update failed for %s", consultation_id)


def _process_one(consultation_id: str) -> None:
    consult_dir = CONSULT_DIR / consultation_id
    try:
        response = generate_bt(consultation_id, timeout_s=JOB_TIMEOUT_S)
    except BTGenerationError as exc:
        _write_user_input_needed_fallback(consultation_id, str(exc))
        return
    except Exception as exc:
        _write_user_input_needed_fallback(consultation_id, f"unexpected: {exc}")
        logger.exception("unexpected worker failure for %s", consultation_id)
        return

    atomic_write_json(consult_dir / "response.json", response)
    meta_path = consult_dir / "metadata.json"
    if meta_path.exists():
        metadata = json.loads(meta_path.read_text())
        metadata["status"] = "complete"
        metadata["responded_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        metadata["responded_by"] = "spark_v2_worker"
        atomic_write_json(meta_path, metadata)


def run_forever(poll_interval_s: float = POLL_INTERVAL_S) -> None:
    logger.info(
        "spark_v2 worker starting: poll=%ss concurrency=%s timeout=%ss",
        poll_interval_s,
        MAX_CONCURRENT_JOBS,
        JOB_TIMEOUT_S,
    )
    in_flight: dict[str, Future] = {}
    with ThreadPoolExecutor(max_workers=MAX_CONCURRENT_JOBS) as pool:
        while True:
            for consultation_id in list(in_flight):
                future = in_flight[consultation_id]
                if future.done():
                    try:
                        future.result()
                    except Exception:
                        logger.exception("worker future raised unexpectedly for %s", consultation_id)
                    del in_flight[consultation_id]

            for consultation_id in _list_pending_consultations():
                if consultation_id in in_flight:
                    continue
                if len(in_flight) >= MAX_CONCURRENT_JOBS:
                    break
                in_flight[consultation_id] = pool.submit(_process_one, consultation_id)

            time.sleep(poll_interval_s)


def use_worker_enabled() -> bool:
    return os.environ.get("TAEY_ED_USE_WORKER", "").strip().lower() in {"1", "true", "yes"}
