"""Create discovery-loop requests for external research harvesters."""

from __future__ import annotations

import logging
import subprocess
import time
from pathlib import Path

from spark_v2.utils.atomic_write import atomic_write_json

logger = logging.getLogger(__name__)

DISCOVERY_DIR = Path("/tmp/taey-ed-discovery")
PROMPT_TEMPLATE_PATH = Path("/home/user/taey-ed/consultations/DEEP_RESEARCH_PROMPT_v2.md")


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _request_id(platform: str) -> str:
    return f"discovery_{platform}_{int(time.time())}"


def create_request(platform_data: dict) -> str:
    platform = str(platform_data.get("platform") or "unknown")
    platform_url = str(platform_data.get("platform_url") or platform)
    platform_type = str(platform_data.get("platform_type") or "other")
    any_context = str(platform_data.get("any_context") or "")
    created_at = _now()
    request_id = _request_id(platform)
    request_dir = DISCOVERY_DIR / request_id
    request_dir.mkdir(parents=True, exist_ok=True)

    template = PROMPT_TEMPLATE_PATH.read_text(encoding="utf-8")
    prompt = (
        template.replace("{PLATFORM_NAME}", platform)
        .replace("{PLATFORM_URL}", platform_url)
        .replace("{PLATFORM_TYPE}", platform_type)
        .replace("{ANY_CONTEXT}", any_context or "none preserved")
    )
    (request_dir / "prompt.txt").write_text(prompt, encoding="utf-8")

    request_payload = {
        "platform": platform,
        "platform_url": platform_url,
        "platform_type": platform_type,
        "request_id": request_id,
        "created_at": created_at,
        "any_context": any_context,
    }
    atomic_write_json(request_dir / "request.json", request_payload)
    atomic_write_json(
        request_dir / "metadata.json",
        {
            "status": "pending",
            "created_at": created_at,
            "request_id": request_id,
            "platform": platform,
        },
    )

    try:
        proc = subprocess.run(
            ["taey-notify", "taeys-hands", f"DISCOVERY: {request_id} for platform={platform}"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        if proc.returncode != 0:
            logger.warning(
                "discovery request notify failed for %s: returncode=%s stderr=%s",
                request_id,
                proc.returncode,
                (proc.stderr or "").strip(),
            )
    except FileNotFoundError:
        logger.warning("discovery request notify unavailable for %s: taey-notify missing", request_id)
    except subprocess.TimeoutExpired:
        logger.warning("discovery request notify timed out for %s", request_id)
    except Exception as exc:
        logger.warning("discovery request notify raised for %s: %s", request_id, exc)

    return request_id
