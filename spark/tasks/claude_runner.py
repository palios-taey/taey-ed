"""Unified Claude CLI runner.

One helper, used by every LLM-using path on the server: bt_generator,
classify_screen, /api/v1/generate. Each call invokes `claude --print` over
the Max subscription (no metered API fees), passes the screenshot as a
file the model is told to Read, and returns the result text plus call
metadata.

Per Jesse 2026-05-12: stay on the CLI subscription until the basic flow is
reliable; SDK-direct + prompt caching is a later optimization once we've
got something working.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Optional, Sequence

logger = logging.getLogger(__name__)

def _resolve_claude_bin() -> str:
    """Resolve the claude CLI robustly. /usr/local/bin/claude vanished mid-run
    2026-06-11 (CLI update relinked to ~/.npm-global) and the worker's systemd
    PATH lost it -> Errno 2 killed BT generation. Try PATH, then known homes;
    fail LOUD with locations tried."""
    import shutil
    found = shutil.which("claude")
    if found:
        return found
    for cand in (os.path.expanduser("~/.npm-global/bin/claude"), "/usr/local/bin/claude"):
        if os.path.exists(cand):
            return cand
    raise ClaudeCallError(
        "claude CLI not found (PATH, ~/.npm-global/bin, /usr/local/bin)"
    )


CLAUDE_BIN = "claude"
# Isolated HOME for headless calls: hook-free settings.json ({}) plus
# symlinked ~/.claude/.credentials.json and ~/.claude.json so OAuth refresh
# propagates. Keeps the fleet's stop-engine/notify hooks out of worker calls.
WORKER_HOME = os.environ.get("TAEY_ED_WORKER_HOME") or os.path.expanduser("~/.taey-worker-home")
DEFAULT_MODEL = "claude-opus-4-8"
DEFAULT_TIMEOUT_S = 180
DEFAULT_MAX_BUDGET_USD = 2.50  # API-equivalent ceiling; Max subscription covers real spend


class ClaudeCallError(RuntimeError):
    """Raised on any failure: timeout, non-zero exit, malformed JSON wrapper,
    `is_error` flag, empty result text. `subtype` carries the CLI result subtype
    (e.g. error_during_execution, error_max_budget_usd) when known, so callers
    can distinguish transient infra errors (retry) from real caps (don't)."""

    def __init__(self, *args, subtype: Optional[str] = None):
        super().__init__(*args)
        self.subtype = subtype


# CLI result subtypes that are TRANSIENT worker/API errors worth a bounded retry
# (vs error_max_budget_usd, a real cap — never retry). error_during_execution is
# the agent loop erroring mid-run, frequently a transient API blip.
_TRANSIENT_SUBTYPES = {"error_during_execution"}


def _build_user_message(user_message: str, screenshot_path: Optional[str]) -> str:
    """If a screenshot is attached, prepend a directive that tells the model
    to actually invoke its Read tool on the file. Without this the model
    sometimes responds blind."""
    if screenshot_path:
        return (
            f"You MUST first use your Read tool to examine this image: "
            f"{screenshot_path}\n\n"
            f"After you have read the image, complete the task below using "
            f"BOTH the image and any other context provided. Do not answer "
            f"without reading the image first.\n\n"
            f"{user_message}"
        )
    return user_message


def call_claude_cli(
    system_prompt: str,
    user_message: str,
    screenshot_path: Optional[str] = None,
    screenshot_b64: Optional[str] = None,
    *,
    model: str = DEFAULT_MODEL,
    timeout_s: float = DEFAULT_TIMEOUT_S,
    max_budget_usd: float = DEFAULT_MAX_BUDGET_USD,
    require_screenshot_read: bool = True,
    permission_mode: str = "bypassPermissions",
    tools: Optional[Sequence[str]] = None,
    add_dirs: Optional[Sequence[str]] = None,
    working_dir: Optional[str] = None,
) -> tuple[str, dict]:
    """Invoke `claude --print` and return (result_text, metadata).

    Args:
        system_prompt: full system prompt (passed via --system-prompt)
        user_message: the user-side instruction
        screenshot_path: optional path to a screenshot the model must inspect
            via its Read tool. Used as-is; not deleted after.
        screenshot_b64: alternative to screenshot_path — pass base64-encoded
            image bytes (PNG or JPEG). We write to a temp file, point the
            model at it, and unlink after the call returns.
        model: model id; defaults to claude-opus-4-7
        timeout_s: wall-clock timeout for the subprocess
        max_budget_usd: API-equivalent ceiling enforced by the CLI. On Max
            subscription this is informational; on metered keys it caps spend.
        require_screenshot_read: when True and an image is attached, verify
            the model actually invoked Read by checking num_turns >= 2. A
            single-turn response on an image-bearing call means it wrote
            blind — raise ClaudeCallError.

    Returns:
        (result_text, metadata) where metadata includes:
            num_turns: int
            duration_ms: int
            total_cost_usd: float  (API-equivalent; $0 on Max subscription)
            session_id: str
            model: str
            elapsed_wall_s: float  (our subprocess wall time)
    """
    if screenshot_path and screenshot_b64:
        raise ValueError("pass either screenshot_path or screenshot_b64, not both")

    # If we were handed b64, write to a temp file so the model can Read it.
    temp_path = None
    temp_dir = None
    if screenshot_b64:
        try:
            image_data = base64.b64decode(screenshot_b64)
        except Exception as e:
            raise ClaudeCallError(f"bad screenshot_b64: {e}") from e
        is_png = image_data[:8] == b"\x89PNG\r\n\x1a\n"
        suffix = ".png" if is_png else ".jpg"
        temp_dir = tempfile.mkdtemp(prefix="taey-claude-image-", dir="/tmp")
        fd, temp_path = tempfile.mkstemp(prefix="image-", suffix=suffix, dir=temp_dir)
        with os.fdopen(fd, "wb") as f:
            f.write(image_data)
        screenshot_path = temp_path

    try:
        # Bounded retry for TRANSIENT worker/API errors (error_during_execution).
        # A transient blip should not consume an escalation tier / paid DR
        # (operator defect 2026-06-14). Real caps (error_max_budget_usd) and all
        # other failures raise immediately — only _TRANSIENT_SUBTYPES retry.
        _max_attempts = 3
        for _attempt in range(1, _max_attempts + 1):
            try:
                return _do_call(
                    system_prompt=system_prompt,
                    user_message=user_message,
                    screenshot_path=screenshot_path,
                    model=model,
                    timeout_s=timeout_s,
                    max_budget_usd=max_budget_usd,
                    require_screenshot_read=require_screenshot_read,
                    permission_mode=permission_mode,
                    tools=tools,
                    add_dirs=add_dirs,
                    working_dir=working_dir,
                )
            except ClaudeCallError as e:
                if getattr(e, "subtype", None) in _TRANSIENT_SUBTYPES and _attempt < _max_attempts:
                    _backoff = 2.0 * _attempt
                    logger.warning(
                        "call_claude_cli transient %s (attempt %d/%d) — retrying in %.1fs",
                        e.subtype, _attempt, _max_attempts, _backoff,
                    )
                    time.sleep(_backoff)
                    continue
                raise
    finally:
        if temp_path:
            try:
                os.unlink(temp_path)
            except OSError:
                pass
        if temp_dir:
            try:
                Path(temp_dir).rmdir()
            except OSError:
                pass


def _do_call(
    system_prompt: str,
    user_message: str,
    screenshot_path: Optional[str],
    model: str,
    timeout_s: float,
    max_budget_usd: float,
    require_screenshot_read: bool,
    permission_mode: str,
    tools: Optional[Sequence[str]],
    add_dirs: Optional[Sequence[str]],
    working_dir: Optional[str],
) -> tuple[str, dict]:
    """The actual subprocess invocation + parsing. Split from call_claude_cli
    so the b64-temp-file lifecycle can wrap it cleanly."""
    full_user = _build_user_message(user_message, screenshot_path)
    if add_dirs is None and screenshot_path and permission_mode != "bypassPermissions":
        add_dirs = [str(Path(screenshot_path).resolve().parent)]

    # NOTHING big rides argv: Linux caps a single argv at 128KB
    # (MAX_ARG_STRLEN) — hit live TWICE on 2026-06-11 (user message 16:33,
    # then the system prompt 16:36, which bt_generator fills with the
    # compiled prompt). User message goes via STDIN; system prompt via
    # --system-prompt-file. No size ceilings anywhere; no-truncation rule
    # honored structurally.
    sys_fd, sys_path = tempfile.mkstemp(prefix="taey-sysprompt-", suffix=".txt", dir="/tmp")
    with os.fdopen(sys_fd, "w") as _sf:
        _sf.write(system_prompt)
    cmd = [
        _resolve_claude_bin(),
        "--print",
        "--output-format", "json",
        "--permission-mode", permission_mode,
        "--model", model,
        "--max-budget-usd", str(max_budget_usd),
        "--system-prompt-file", sys_path,
    ]
    if tools is not None:
        cmd.extend(["--tools", ",".join(tools)])
    if add_dirs:
        for path in add_dirs:
            cmd.extend(["--add-dir", path])

    # Isolated HOME: hook-free settings + symlinked credentials. The fleet's
    # stop-engine hooks otherwise fire INSIDE headless calls — observed live
    # 2026-06-11 12:16: the worker emitted its BT, the Stop hook hijacked the
    # final turn, and --print returned orchestration chatter ("taey-stop-reason
    # status reports can_stop: true...") instead of BT JSON. Also the likely
    # cause of today's intermittent exit-1/empty responses. (--bare would skip
    # hooks too but drops OAuth with it.)
    # DISABLE_AUTOUPDATER: the CLI auto-updated itself mid-run (16:09) and
    # the binary vanished for the duration of the relink — a worker call
    # raced it and died. Production loops must not race auto-updates.
    worker_env = {**os.environ, "HOME": WORKER_HOME, "DISABLE_AUTOUPDATER": "1"}

    t0 = time.time()
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout_s,
            input=full_user,
            env=worker_env,
            cwd=working_dir,
        )
    except subprocess.TimeoutExpired as e:
        raise ClaudeCallError(
            f"claude --print timed out after {timeout_s}s"
        ) from e
    finally:
        try:
            os.unlink(sys_path)
        except OSError:
            pass
    elapsed = time.time() - t0

    if result.returncode != 0:
        raise ClaudeCallError(
            f"claude exit {result.returncode}: stderr={result.stderr[:500]} "
            f"stdout={result.stdout[:500]}"
        )

    try:
        outer = json.loads(result.stdout)
    except json.JSONDecodeError as e:
        raise ClaudeCallError(
            f"claude stdout not JSON: {e}; head={result.stdout[:300]}"
        ) from e

    if outer.get("is_error"):
        _subtype = outer.get("subtype")
        # CAPTURE for diagnosis (operator defect 2026-06-14: error_during_execution
        # left nothing diagnosable). Persist the FULL worker result + stderr next
        # to the handoff so the failure is inspectable, and log richly.
        try:
            if working_dir:
                (Path(working_dir) / "worker_error.json").write_text(
                    json.dumps(
                        {"outer": outer, "stderr": result.stderr, "elapsed_s": elapsed},
                        indent=2, ensure_ascii=False,
                    ),
                    encoding="utf-8",
                )
        except Exception:
            logger.exception("failed to persist worker_error.json")
        logger.error(
            "call_claude_cli is_error: subtype=%s session=%s num_turns=%s "
            "result=%r stderr=%r full_outer=%s",
            _subtype, outer.get("session_id"), outer.get("num_turns"),
            (outer.get("result") or "")[:500], (result.stderr or "")[:500],
            json.dumps(outer)[:1500],
        )
        raise ClaudeCallError(
            f"claude reported error: subtype={_subtype} "
            f"result={(outer.get('result') or '')[:300]} "
            f"stderr={(result.stderr or '')[:200]}",
            subtype=_subtype,
        )

    text = outer.get("result", "")
    if not text:
        raise ClaudeCallError("claude returned empty result")

    metadata = {
        "num_turns": outer.get("num_turns", 0),
        "duration_ms": outer.get("duration_ms", 0),
        "total_cost_usd": outer.get("total_cost_usd", 0.0),
        "session_id": outer.get("session_id", ""),
        "model": model,
        "elapsed_wall_s": elapsed,
        "permission_mode": permission_mode,
        "tools": list(tools or []),
        "add_dirs": [str(Path(p)) for p in (add_dirs or [])],
        "working_dir": str(Path(working_dir)) if working_dir else "",
    }

    # Verify the model actually invoked Read on the screenshot. A single-turn
    # response means it produced text without using any tools — i.e. it wrote
    # the answer blind. That's the silent failure mode that surfaced as
    # "screen_type=UNKNOWN" on Khan biology this morning.
    if screenshot_path and require_screenshot_read:
        num_turns = metadata["num_turns"]
        if num_turns < 2:
            raise ClaudeCallError(
                f"claude did not invoke Read on screenshot {screenshot_path} "
                f"(num_turns={num_turns}); answer would be blind"
            )

    logger.info(
        f"call_claude_cli ok: model={model} turns={metadata['num_turns']} "
        f"elapsed={elapsed:.1f}s api_eq_cost=${metadata['total_cost_usd']:.3f} "
        f"result_len={len(text)}"
    )
    return text, metadata
