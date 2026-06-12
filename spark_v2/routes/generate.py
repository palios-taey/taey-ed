"""Claude-backed answer generation route for BT send_to_llm calls."""

from __future__ import annotations

import base64
import json
import re
import subprocess
import time
import uuid
from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from spark_v2.utils.atomic_write import atomic_write_json

router = APIRouter()

GENERATE_DIR_PREFIX = "/tmp/taey-ed-generate-"
CONSULTATIONS_DIR = Path("/home/user/taey-ed/consultations")
SOLVE_PROMPT_PATH = CONSULTATIONS_DIR / "SOLVE_PROMPT_v1.md"
DEFAULT_MODEL = "claude-opus-4-7"
DEFAULT_TIMEOUT_S = 180.0
DEFAULT_MAX_BUDGET_USD = 2.5


def _json_compact(value: object) -> str:
    return json.dumps(value, separators=(",", ":"), ensure_ascii=True)


def _read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _extract_named_section(markdown: str, heading: str, level: int = 2) -> str:
    pattern = rf"^{'#' * level} {re.escape(heading)}$"
    match = re.search(pattern, markdown, flags=re.MULTILINE)
    if not match:
        raise ValueError(f"Section heading not found: {heading}")
    start = match.end()
    next_match = re.search(rf"^#{{1,{level}}} .*$", markdown[start:], flags=re.MULTILINE)
    end = start + next_match.start() if next_match else len(markdown)
    return markdown[start:end].strip()


def _extract_fenced_block(text: str, language: str) -> str:
    match = re.search(rf"```{re.escape(language)}\n(.*?)\n```", text, flags=re.DOTALL)
    if not match:
        raise ValueError(f"Fenced {language} block not found")
    return match.group(1).strip()


def _load_solve_prompt_spec(path: Path = SOLVE_PROMPT_PATH) -> dict:
    markdown = _read_text(path)
    system_prompt = _extract_fenced_block(_extract_named_section(markdown, "System Prompt", level=2), "text")
    user_sections = json.loads(
        _extract_fenced_block(_extract_named_section(markdown, "User Message Sections", level=2), "json")
    )
    taxonomy = json.loads(
        _extract_fenced_block(_extract_named_section(markdown, "Question-Type Taxonomy", level=2), "json")
    )
    if not isinstance(user_sections, dict) or not isinstance(taxonomy, dict):
        raise ValueError("Solve prompt spec is malformed")
    return {
        "system_prompt": system_prompt,
        "user_sections": {str(key): str(value) for key, value in user_sections.items()},
        "taxonomy": taxonomy,
    }


SOLVE_PROMPT_SPEC = _load_solve_prompt_spec()


def _extract_json_object(text: str) -> str:
    raw = text.strip()
    if raw.startswith("```"):
        lines = raw.splitlines()
        if lines and lines[-1].strip() == "```":
            raw = "\n".join(lines[1:-1]).strip()
        else:
            raw = "\n".join(lines[1:]).strip()
    start = raw.find("{")
    if start < 0:
        raise ValueError("no JSON object found in Claude output")
    depth = 0
    in_string = False
    escape = False
    for index in range(start, len(raw)):
        char = raw[index]
        if escape:
            escape = False
            continue
        if in_string:
            if char == "\\":
                escape = True
            elif char == "\"":
                in_string = False
            continue
        if char == "\"":
            in_string = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return raw[start:index + 1]
    raise ValueError("unbalanced JSON object in Claude output")


def _build_user_message(payload: dict) -> str:
    templates = SOLVE_PROMPT_SPEC["user_sections"]
    sections = [
        templates["question_text"].replace("{QUESTION}", str(payload.get("question") or "")),
    ]
    if payload.get("options") is not None:
        sections.append(
            templates["options"].replace("{OPTIONS_JSON}", _json_compact(payload.get("options")))
        )
    if payload.get("items") is not None:
        sections.append(
            templates["items"].replace("{ITEMS_JSON}", _json_compact(payload.get("items")))
        )
    if payload.get("context"):
        sections.append(
            templates["reference_context"].replace("{CONTEXT_JSON}", _json_compact(payload.get("context")))
        )
    sections.append(templates["screenshot"])
    if payload.get("image_descriptions"):
        sections.append(
            templates["image_descriptions"].replace(
                "{IMAGE_DESCRIPTIONS_JSON}",
                _json_compact(payload.get("image_descriptions")),
            )
        )
    sections.append(
        templates["solve_contract"]
        .replace("{QUESTION_TYPE}", str(payload.get("question_type") or ""))
        .replace("{HAS_TEXT_FIELD}", str(bool(payload.get("has_text_field"))))
    )
    return "\n\n".join(sections)


def _build_stream_input(user_message: str, screenshot_bytes: bytes) -> str:
    return json.dumps(
        {
            "type": "user",
            "message": {
                "role": "user",
                "content": [
                    {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": "image/png",
                            "data": base64.b64encode(screenshot_bytes).decode("ascii"),
                        },
                    },
                    {
                        "type": "text",
                        "text": user_message,
                    },
                ],
            },
        },
        ensure_ascii=True,
    ) + "\n"


def _call_claude(system_prompt_path: Path, user_message: str, screenshot_bytes: bytes) -> dict:
    cmd = [
        "claude",
        "--print",
        "--input-format",
        "stream-json",
        "--output-format",
        "stream-json",
        "--verbose",
        "--permission-mode",
        "bypassPermissions",
        "--model",
        DEFAULT_MODEL,
        "--max-budget-usd",
        str(DEFAULT_MAX_BUDGET_USD),
        "--system-prompt-file",
        str(system_prompt_path),
    ]
    result = subprocess.run(
        cmd,
        input=_build_stream_input(user_message, screenshot_bytes),
        capture_output=True,
        text=True,
        timeout=DEFAULT_TIMEOUT_S,
    )
    if result.returncode != 0:
        raise RuntimeError(f"claude exited {result.returncode}: {result.stderr[:500]}")
    events: list[dict] = []
    for line in result.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        event = json.loads(line)
        if isinstance(event, dict):
            events.append(event)
    outer = next((event for event in reversed(events) if event.get("type") == "result"), None)
    if outer is None:
        raise RuntimeError("claude stream did not produce a result event")
    if outer.get("subtype") == "error" or outer.get("is_error"):
        raise RuntimeError(f"claude reported error: {(outer.get('result') or '')[:300]}")
    candidate = _extract_json_object(str(outer.get("result") or ""))
    parsed = json.loads(candidate)
    parsed["_call_metadata"] = {
        "num_turns": outer.get("num_turns", 0),
        "duration_ms": outer.get("duration_ms", 0),
        "total_cost_usd": outer.get("total_cost_usd", 0.0),
        "session_id": outer.get("session_id", ""),
        "model": DEFAULT_MODEL,
        "system_prompt_flag": "stream-json",
        "system_prompt_source": "--system-prompt-file",
    }
    return parsed


def _validate_result(question_type: str, parsed: dict) -> None:
    if parsed.get("success") is not True:
        if not parsed.get("error"):
            raise ValueError("unsuccessful response missing error")
        return
    taxonomy = SOLVE_PROMPT_SPEC["taxonomy"].get("question_types", {})
    spec = taxonomy.get(question_type)
    if not isinstance(spec, dict):
        raise ValueError(f"unsupported question_type: {question_type}")
    required_keys = spec.get("required_keys", {})
    if not isinstance(required_keys, dict) or not required_keys:
        raise ValueError(f"question_type spec is malformed: {question_type}")
    for key, validator in required_keys.items():
        value = parsed.get(key)
        if validator == "non_empty_string":
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"successful {question_type} response missing {key}")
            continue
        if validator == "non_empty_list":
            if not isinstance(value, list) or not value:
                raise ValueError(f"successful {question_type} response missing {key}")
            continue
        if validator == "non_empty_dict":
            if not isinstance(value, dict) or not value:
                raise ValueError(f"successful {question_type} response missing {key}")
            continue
        raise ValueError(f"unsupported validator for {question_type}.{key}: {validator}")


@router.post("/api/v1/generate")
async def generate(request: Request) -> JSONResponse:
    request_id = uuid.uuid4().hex
    request_dir = Path(f"{GENERATE_DIR_PREFIX}{request_id}")
    request_dir.mkdir(parents=True, exist_ok=True)
    try:
        payload = await request.json()
        if not isinstance(payload, dict):
            raise ValueError("request body must be a JSON object")

        screenshot_b64 = str(payload.get("screenshot_b64") or "")
        if not screenshot_b64:
            raise ValueError("missing screenshot_b64")

        screenshot_bytes = base64.b64decode(screenshot_b64)
        screenshot_path = request_dir / "screenshot.png"
        screenshot_path.write_bytes(screenshot_bytes)

        system_prompt_path = request_dir / "system_prompt.txt"
        system_prompt_path.write_text(SOLVE_PROMPT_SPEC["system_prompt"], encoding="utf-8")

        request_payload = dict(payload)
        request_payload["_request_id"] = request_id
        request_payload["_created_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        request_payload["_solve_prompt_path"] = str(SOLVE_PROMPT_PATH)
        atomic_write_json(request_dir / "request.json", request_payload)

        user_message = _build_user_message(payload)
        (request_dir / "prompt.txt").write_text(user_message, encoding="utf-8")

        parsed = _call_claude(system_prompt_path, user_message, screenshot_bytes)
        response_payload = dict(parsed)
        response_payload.pop("_call_metadata", None)
        _validate_result(str(payload.get("question_type") or ""), response_payload)
        artifact_payload = dict(response_payload)
        artifact_payload["_call_metadata"] = parsed.get("_call_metadata", {})
        atomic_write_json(request_dir / "response.json", artifact_payload)
        return JSONResponse(status_code=200, content=response_payload)
    except Exception as exc:
        error_payload = {"success": False, "error": str(exc)}
        atomic_write_json(request_dir / "response.json", error_payload)
        return JSONResponse(status_code=500, content=error_payload)
