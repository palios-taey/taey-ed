"""Consultation worker: poll loop that picks up pending consultations and
generates BTs via Claude CLI.

Architecture (per LAUNCH_PLAN.md Phase 2 + ChatGPT review):
  - Polls /tmp/taey-ed-consult/ for consultations with status="pending"
  - For each: invokes bt_generator.generate_bt() (subprocess to claude --print)
  - On success: writes response.json + flips metadata.status to "complete"
  - On failure: writes a user_input_needed BT response so the Mac doesn't hang
  - Concurrency cap to avoid one hung Claude blocking the queue
  - Per-job timeout
  - Cost/budget logging

Run as a separate process from the FastAPI server:
    python -m spark.worker.run
or under systemd / supervisor in production.

For DEV / Jesse's interactive testing, this worker should NOT run alongside
the tmux Spark-Claude path or both will race to produce response.json. The
env flag TAEY_ED_USE_WORKER=1 in consultation_request.py disables tmux notify
when set.
"""

import json
import logging
import os
import time
import base64
from concurrent.futures import ThreadPoolExecutor, Future
from datetime import datetime
from pathlib import Path
from typing import Optional

from spark.tasks.atomic_write import atomic_write_json
from spark.tasks.classification_request import CLASSIFY_DIR
from spark.tasks.classify_screen import classify_screen
from spark.worker.bt_generator import BTGenerationError, generate_bt

logger = logging.getLogger(__name__)

CONSULT_DIR = Path("/tmp/taey-ed-consult")
POLL_INTERVAL_S = 2.0
MAX_CONCURRENT_JOBS = 3  # bounded so one hung Claude doesn't stall the queue
JOB_TIMEOUT_S = 300.0  # Claude --print with full prompt_codex prompt can take 2-4 min
PENDING_TTL_SECONDS = 600


def _state_evidence(source: str, **extra) -> dict:
    return {"source": f"consultation_worker.{source}", **extra}


def _state_repo():
    from spark.state_repo import get_state_repo
    return get_state_repo()


def _read_metadata(path: Path) -> dict:
    return json.loads(path.read_text())


def _pending_metadata_age_seconds(meta: dict) -> float | None:
    ts = meta.get("timestamp")
    if not ts:
        return None
    try:
        return max(0.0, time.time() - datetime.fromisoformat(ts).timestamp())
    except Exception:
        return None


def _state_consult_is_pending(consultation_id: str) -> bool:
    try:
        row = _state_repo().get_consult_status(consultation_id)
    except Exception:
        logger.exception("worker: state-store pending check failed for %s", consultation_id)
        return False
    if not row:
        logger.warning("worker: ignoring file-only pending consult %s (no state row)", consultation_id)
        return False
    if row.get("status") != "pending":
        logger.info(
            "worker: ignoring consult %s because state row is %s",
            consultation_id,
            row.get("status"),
        )
        return False
    return True


def _mirror_worker_failed(consultation_id: str, reason: str, source: str) -> None:
    try:
        _state_repo().resolve_consult(
            consult_id=consultation_id,
            status="worker_failed",
            actor="worker",
            evidence=_state_evidence(source),
            failure_reason=reason,
        )
    except Exception:
        logger.exception("state-store dual-write failed: consultation_worker.%s", source)


def _mirror_worker_complete(consultation_id: str, meta: dict, bt: dict) -> None:
    repo = _state_repo()
    try:
        screen_hash = meta.get("screen_hash")
        platform = meta.get("platform")
        if platform and screen_hash and bt.get("tree"):
            repo.record_behavior_tree(
                platform=platform,
                key_kind="skeleton",
                key_hash=screen_hash,
                bt_json=bt["tree"],
                built_by="worker",
                source_kind="consultation_worker",
                actor="worker",
                evidence=_state_evidence("process_one", consultation_id=consultation_id),
                screen_type=bt.get("screen_type"),
                bundle={"consultation_id": consultation_id, "screen_type": bt.get("screen_type")},
            )
    except Exception:
        logger.exception("state-store dual-write failed: consultation_worker.process_one.bt")
    try:
        repo.resolve_consult(
            consult_id=consultation_id,
            status="complete",
            actor="worker",
            evidence=_state_evidence("process_one"),
        )
    except Exception:
        logger.exception("state-store dual-write failed: consultation_worker.process_one.consult")


def _mirror_classification_job(job_ref: str, status: str, source: str, result: dict | None = None) -> None:
    try:
        meta = _read_metadata(CLASSIFY_DIR / job_ref / "metadata.json")
        _state_repo().record_classification_job(
            platform=meta["platform"],
            skel_hash=meta["skeleton_hash"],
            classification_id=meta.get("classification_id", Path(job_ref).name),
            status=status,
            result=result,
            actor="worker",
            evidence=_state_evidence(source),
        )
    except Exception:
        logger.exception("state-store dual-write failed: consultation_worker.%s", source)


def _list_pending_consultations() -> list[str]:
    """Return consultation IDs whose metadata.status is 'pending'."""
    if not CONSULT_DIR.exists():
        return []
    pending = []
    for sub in CONSULT_DIR.iterdir():
        if not sub.is_dir() or not sub.name.startswith("consult_"):
            continue
        meta_path = sub / "metadata.json"
        response_path = sub / "response.json"
        if not meta_path.exists():
            continue
        # Skip if a response already exists (another path beat us to it,
        # or a prior worker run already processed it)
        if response_path.exists():
            continue
        try:
            meta = json.loads(meta_path.read_text())
        except (json.JSONDecodeError, OSError):
            continue
        if meta.get("status") == "pending":
            age = _pending_metadata_age_seconds(meta)
            if age is not None and age > PENDING_TTL_SECONDS:
                logger.warning(
                    "worker: ignoring stale pending consult %s (age=%ss)",
                    sub.name,
                    int(age),
                )
                continue
            if not _state_consult_is_pending(sub.name):
                continue
            pending.append(sub.name)
    return pending


def _list_pending_classifications() -> list[str]:
    if not CLASSIFY_DIR.exists():
        return []
    pending = []
    for platform_dir in CLASSIFY_DIR.iterdir():
        if not platform_dir.is_dir():
            continue
        for sub in platform_dir.iterdir():
            if not sub.is_dir() or not sub.name.startswith("classify_"):
                continue
            meta_path = sub / "metadata.json"
            response_path = sub / "response.json"
            if not meta_path.exists() or response_path.exists():
                continue
            try:
                meta = json.loads(meta_path.read_text())
            except (json.JSONDecodeError, OSError):
                continue
            if meta.get("status") == "pending":
                pending.append(str(sub.relative_to(CLASSIFY_DIR)))
    return pending


def _write_user_input_needed_fallback(
    consultation_id: str,
    reason: str,
    *,
    failure_kind: str = "worker_pipeline",
    rejected_bt_path: str | None = None,
) -> None:
    """If generation fails, write a fallback response that surfaces back to
    the Mac as user_input_needed. Step 1 of /next_action checks the
    _worker_fallback flag and converts to a user_input_needed directive
    instead of execute_tree-ing the (intentionally inert) wait BT here.

    The inert wait BT is kept as the `tree` payload so any legacy reader
    that still looks at it sees a no-op rather than crashing on a missing
    key — but the contract is: Step 1 must read `_worker_fallback` first.
    """
    consult_dir = CONSULT_DIR / consultation_id
    meta_path = consult_dir / "metadata.json"
    meta = {}
    try:
        meta = _read_metadata(meta_path)
    except Exception:
        pass
    fallback = {
        "tree": {
            "type": "action",
            "action": "wait",
            "params": {"seconds": 5.0},
        },
        "screen_type": meta.get("screen_type_hint") or "UNKNOWN",
        "expected_next": [],
        "extract": None,
        "_worker_fallback": True,
        "_worker_failure_reason": reason,
        "_worker_failure_kind": failure_kind,
    }
    if rejected_bt_path:
        fallback["_rejected_bt_path"] = rejected_bt_path
    atomic_write_json(consult_dir / "response.json", fallback)
    if meta:
        meta["status"] = "worker_failed"
        meta["worker_failure_reason"] = reason
        meta["worker_failure_kind"] = failure_kind
        if rejected_bt_path:
            meta["rejected_bt_path"] = rejected_bt_path
        try:
            atomic_write_json(meta_path, meta)
        except Exception:
            pass
    _mirror_worker_failed(consultation_id, reason, "write_user_input_needed_fallback")
    logger.error(
        f"worker: wrote fallback for {consultation_id} "
        f"(kind={failure_kind}, reason: {reason})"
    )


def _write_classification_fallback(job_ref: str, reason: str) -> None:
    job_dir = CLASSIFY_DIR / job_ref
    fallback = {
        "success": False,
        "screen_type": "UNKNOWN",
        "confidence_note": f"classification_worker_error: {reason}",
        "platform_variant": "",
    }
    atomic_write_json(job_dir / "response.json", fallback)
    meta_path = job_dir / "metadata.json"
    try:
        meta = _read_metadata(meta_path)
        meta["status"] = "worker_failed"
        meta["worker_failure_reason"] = reason
        atomic_write_json(meta_path, meta)
    except Exception:
        pass
    _mirror_classification_job(job_ref, "worker_failed", "write_classification_fallback", fallback)
    logger.error("worker: wrote classification fallback for %s (%s)", job_ref, reason)


def _process_one(consultation_id: str) -> None:
    """Process a single consultation. Catches all errors and writes a fallback
    on failure so the Mac never hangs."""
    consult_dir = CONSULT_DIR / consultation_id
    t0 = time.time()
    try:
        bt = generate_bt(consultation_id, timeout_s=JOB_TIMEOUT_S)
    except BTGenerationError as e:
        _write_user_input_needed_fallback(
            consultation_id,
            str(e),
            failure_kind=getattr(e, "failure_kind", "worker_pipeline"),
            rejected_bt_path=getattr(e, "rejected_bt_path", None),
        )
        return
    except Exception as e:
        _write_user_input_needed_fallback(consultation_id, f"unexpected: {e}")
        logger.exception(f"worker: unexpected error for {consultation_id}")
        return

    # Persist the BT response + mark consultation complete.
    response_path = consult_dir / "response.json"
    atomic_write_json(response_path, bt)
    meta_path = consult_dir / "metadata.json"
    try:
        meta = _read_metadata(meta_path)
        meta["status"] = "complete"
        meta["responded_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        meta["responded_by"] = "worker"
        atomic_write_json(meta_path, meta)
        _mirror_worker_complete(consultation_id, meta, bt)
    except Exception as e:
        logger.warning(
            f"worker: response.json written for {consultation_id} but "
            f"metadata update failed: {e}"
        )

    elapsed = time.time() - t0
    logger.info(
        f"worker: processed {consultation_id} in {elapsed:.1f}s "
        f"(screen_type={bt.get('screen_type')})"
    )


def _process_one_classification(job_ref: str) -> None:
    job_dir = CLASSIFY_DIR / job_ref
    t0 = time.time()
    try:
        meta = json.loads((job_dir / "metadata.json").read_text())
        tree = json.loads((job_dir / "tree.json").read_text())
        screenshot_path = job_dir / "screenshot.png"
        screenshot_b64 = None
        if screenshot_path.exists():
            screenshot_b64 = base64.b64encode(screenshot_path.read_bytes()).decode("ascii")

        result = classify_screen(
            tree=tree,
            screenshot_b64=screenshot_b64,
            platform=meta["platform"],
        )
    except Exception as e:
        _write_classification_fallback(job_ref, f"unexpected: {e}")
        logger.exception("worker: unexpected classification error for %s", job_ref)
        return

    atomic_write_json(job_dir / "response.json", result)
    meta_path = job_dir / "metadata.json"
    try:
        meta = _read_metadata(meta_path)
        meta["status"] = "complete"
        meta["responded_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        meta["responded_by"] = "worker"
        atomic_write_json(meta_path, meta)
        _mirror_classification_job(job_ref, "complete", "process_one_classification", result)
    except Exception as e:
        logger.warning(
            "worker: classification response written for %s but metadata update failed: %s",
            job_ref,
            e,
        )

    elapsed = time.time() - t0
    logger.info(
        "worker: classified %s in %.1fs (screen_type=%s)",
        job_ref,
        elapsed,
        result.get("screen_type"),
    )


def run_forever(poll_interval_s: float = POLL_INTERVAL_S) -> None:
    """Main worker loop. Polls for pending consultations and processes them
    concurrently up to MAX_CONCURRENT_JOBS.
    """
    logger.info(
        f"consultation worker starting: poll={poll_interval_s}s, "
        f"concurrency={MAX_CONCURRENT_JOBS}, job_timeout={JOB_TIMEOUT_S}s"
    )
    in_flight: dict[str, Future] = {}
    with ThreadPoolExecutor(max_workers=MAX_CONCURRENT_JOBS) as pool:
        while True:
            # Reap completed futures so the in_flight dict doesn't leak
            for cid in list(in_flight.keys()):
                fut = in_flight[cid]
                if fut.done():
                    # Future.result() re-raises any exception in the worker
                    # thread; but _process_one catches its own errors, so this
                    # should never raise. Defensive try/except for any
                    # truly-unexpected case.
                    try:
                        fut.result()
                    except Exception:
                        logger.exception(
                            f"worker: future for {cid} raised unexpectedly"
                        )
                    del in_flight[cid]

            # Prioritize classification first so /next_action can answer the
            # next poll without holding the HTTP request open.
            for job_ref in _list_pending_classifications():
                key = f"classify:{job_ref}"
                if key in in_flight:
                    continue
                if len(in_flight) >= MAX_CONCURRENT_JOBS:
                    break
                logger.info(f"worker: dispatching classification {job_ref}")
                in_flight[key] = pool.submit(_process_one_classification, job_ref)

            # Find new consultation work
            for cid in _list_pending_consultations():
                key = f"consult:{cid}"
                if key in in_flight:
                    continue
                if len(in_flight) >= MAX_CONCURRENT_JOBS:
                    break
                logger.info(f"worker: dispatching {cid}")
                in_flight[key] = pool.submit(_process_one, cid)

            time.sleep(poll_interval_s)


def use_worker_enabled() -> bool:
    """True iff TAEY_ED_USE_WORKER=1 is set in env. When set, the
    consultation_request module skips tmux notify and the worker is
    expected to pick up the consultation."""
    return os.environ.get("TAEY_ED_USE_WORKER", "").strip() in ("1", "true", "yes")
