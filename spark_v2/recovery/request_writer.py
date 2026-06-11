"""Create recovery-loop requests for external research harvesters."""

from __future__ import annotations

import json
import logging
import subprocess
import time
import uuid
from pathlib import Path

from spark_v2.tasks.knowledge_loader import failed_provisional_attempts
from spark_v2.utils.atomic_write import atomic_write_json

logger = logging.getLogger(__name__)

RECOVERY_DIR = Path("/tmp/taey-ed-recovery")
PROMPT_TEMPLATE_PATH = Path("/home/user/taey-ed/consultations/RECOVERY_RESEARCH_PROMPT_v2.md")


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _request_id(platform: str) -> str:
    return f"recovery_{platform}_{int(time.time())}_{uuid.uuid4().hex[:6]}"


def _pretty(value) -> str:
    return json.dumps(value, indent=2, ensure_ascii=True, sort_keys=True)


def _render_failed_attempts(provisional_data: dict | None) -> str:
    attempts = failed_provisional_attempts(provisional_data)
    if not attempts:
        return "[]"
    return _pretty(attempts)


def create_request(request_data: dict) -> str:
    platform = str(request_data.get("platform") or "unknown")
    request_id = _request_id(platform)
    request_dir = RECOVERY_DIR / request_id
    request_dir.mkdir(parents=True, exist_ok=True)

    template = PROMPT_TEMPLATE_PATH.read_text(encoding="utf-8")
    replacements = {
        "{PLATFORM_DISPLAY_NAME}": str(request_data.get("platform_display_name") or platform),
        "{PLATFORM_URL}": str(request_data.get("platform_url") or platform),
        "{SCREEN_TYPE}": str(request_data.get("screen_type") or "UNKNOWN"),
        "{TIER1_TIMESTAMP}": str(request_data.get("tier1_timestamp") or _now()),
        "{ATTEMPT_COUNT}": str(request_data.get("attempt_count") or 0),
        "{COURSE_ID}": str(request_data.get("course_id") or ""),
        "{TREE_EXTRACTION}": _pretty(request_data.get("tree") or {}),
        "{VISION_EXTRACTION}": str(request_data.get("vision_extraction") or "unknown"),
        "{MISMATCH_HYPOTHESIS}": str(request_data.get("mismatch_hypothesis") or "unknown"),
        "{FAILED_BT_JSON}": _pretty(request_data.get("failed_bt") or {}),
        "{BT_DEBUG_TAIL}": str(request_data.get("bt_debug_tail") or ""),
        "{KNOWLEDGE_JSON_BODY}": _pretty(request_data.get("knowledge") or {}),
        "{PROVISIONAL_KNOWLEDGE_JSON_BODY}": _pretty(request_data.get("provisional") or {}),
        "{FAILED_PROVISIONAL_ATTEMPTS}": _render_failed_attempts(request_data.get("provisional")),
        "{platform_slug}": platform,
    }
    prompt = template
    for needle, value in replacements.items():
        prompt = prompt.replace(needle, value)
    (request_dir / "prompt.txt").write_text(prompt, encoding="utf-8")

    request_payload = dict(request_data)
    request_payload["request_id"] = request_id
    request_payload["created_at"] = _now()
    atomic_write_json(request_dir / "request.json", request_payload)
    atomic_write_json(
        request_dir / "metadata.json",
        {
            "status": "pending",
            "created_at": request_payload["created_at"],
            "request_id": request_id,
            "platform": platform,
            "screen_type": request_payload.get("screen_type") or "UNKNOWN",
        },
    )

    try:
        proc = subprocess.run(
            ["taey-notify", "taeys-hands", f"RECOVERY: {request_id} for platform={platform}"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        if proc.returncode != 0:
            logger.warning(
                "recovery request notify failed for %s: returncode=%s stderr=%s",
                request_id,
                proc.returncode,
                (proc.stderr or "").strip(),
            )
    except FileNotFoundError:
        logger.warning("recovery request notify unavailable for %s: taey-notify missing", request_id)
    except subprocess.TimeoutExpired:
        logger.warning("recovery request notify timed out for %s", request_id)
    except Exception as exc:
        logger.warning("recovery request notify raised for %s: %s", request_id, exc)

    return request_id
