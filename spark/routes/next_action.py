"""
/next_action — V21 Directive Model.

Mac sends state, Spark returns ONE directive. All intelligence server-side.
Mac is a dumb executor; Spark makes all decisions.

Flow:
  1. Active consultation? → wait or execute completed BT
  2. Validate previous action → stuck detection, wrong-answer detection
  3. BT failure recovery → retry with context, delete bad hash mappings
  4. V21 Exact hash lookup → skeleton hash → variant → stored BT (free, ~0ms)
  5. No hash match → knowledge gate → Flash classify ($0.002) → variant BT cache → Pro ($0.09)

Screen types (6 universal + 1 escalation, IMS Caliper validated):
  NAVIGATION, VIDEO, ARTICLE, EXERCISE, TRANSITION, UNKNOWN

V21 (2026-02-28):
  - Replaced Jaccard signature matching with skeleton hash + Flash classification
  - Skeleton scoped to AXWebArea (excludes browser chrome)
  - Variant BT cache: BTs stored per variant, not per screen instance
  - Non-deterministic variants (EXERCISE_*) always get fresh Pro BT
  - Data files under TAEY_ED_DATA_DIR (see spark/tasks/paths.py)
"""

import json
import logging
import time
import uuid
from pathlib import Path

from fastapi import APIRouter

from spark.models import NextActionRequest, ClientState
from spark.tasks.load_yaml import load_yaml
from spark.tasks.handle_consultation import (
    request_consultation,
    check_consultation,
)
from spark.tasks.chat_store import (
    store_message,
    build_status,
    build_question,
    build_user_message,
)

logger = logging.getLogger(__name__)

router = APIRouter()

# ── Claude-primary platforms ──
# Platforms in this set bypass Flash classify + Pro BT-build entirely and route
# screens directly to the taey-ed tmux session (Spark Claude) for vision +
# BT-building. Reversible by removing the platform from the set.
CLAUDE_PRIMARY_PLATFORMS = {"khan_academy"}


def _escalate_to_claude_diagnosing(
    platform: str,
    tree: dict | None,
    consultation_id: str,
    reason: str,
    screen_type_hint: str = "UNKNOWN",
    bt_debug_tail: str = "",
    failed_bt: dict | None = None,
    screenshot_b64: str | None = None,
) -> dict:
    """Route any failure through the Mira-side claude diagnosis loop.

    Per Jesse's directive (2026-05-18): the Mac app + API do not escalate to
    user. Everything that isn't perfectly handled goes to claude-on-Mira.
    State is keyed by (platform, skeleton_hash) so reconsults across multiple
    consult_ids see the same diagnosis state. Pings claude once per stuck-screen
    cycle; returns a wait directive so Mac keeps polling without surfacing a
    dialog. Only escalates to user_input_needed when claude EXPLICITLY touches
    the gave_up.flag in the state dir — Mac/Spark never make that decision.

    Persistence (2026-05-19 bug fix): tree and screenshot_b64 are written to
    the diag_dir as tree.json / screenshot.png so the escalation packet
    builder can read them. Required for the new Step 4.5 → claude-primary
    escalation path which has no consult_id (and therefore no consult dir
    with these artifacts).
    """
    try:
        from spark.tasks.skeleton import (
            extract_skeleton as _ext_sk, skeleton_hash as _skel_hash,
        )
        _screen_hash = _skel_hash(_ext_sk(tree)) if tree else "unknown"
    except Exception:
        _screen_hash = "unknown"

    diag_dir = Path("/tmp/taey-ed-claude-diagnosing") / f"{platform}_{_screen_hash[:16]}"
    diag_dir.mkdir(parents=True, exist_ok=True)
    diagnosing = diag_dir / "diagnosing.flag"
    done = diag_dir / "diagnosis_done.flag"
    gave_up = diag_dir / "gave_up.flag"
    retry_p = diag_dir / "retries.txt"

    # Persist tree + screenshot to diag_dir so the escalation packet builder
    # has the AX data. Write only on first entry (idempotent — don't overwrite
    # if Mac's tree mutated between escalations on the same hash).
    if tree and not (diag_dir / "tree.json").exists():
        try:
            (diag_dir / "tree.json").write_text(json.dumps(tree, indent=2))
        except Exception:
            logger.exception("escalate: failed to write tree.json")
    if screenshot_b64 and not (diag_dir / "screenshot.png").exists():
        try:
            import base64
            (diag_dir / "screenshot.png").write_bytes(base64.b64decode(screenshot_b64))
        except Exception:
            logger.exception("escalate: failed to write screenshot.png")

    # Per-hash user-fallback override (Jesse 2026-05-19): if this hash is in
    # /home/user/taey-ed-data/user_fallback_hashes.txt, route directly to
    # user. Checked HERE (in the escalation function) so it fires regardless
    # of which step triggered the escalation — including Step 3 failure path
    # where the hash has already been deleted from hash_index.
    try:
        _override_path = Path("/home/user/taey-ed-data/user_fallback_hashes.txt")
        if _override_path.exists():
            _flagged = _override_path.read_text()
            if _screen_hash in _flagged or _screen_hash[:12] in _flagged:
                logger.info(
                    f"  escalate: hash {_screen_hash[:12]} on user-fallback override — routing to user"
                )
                return _with_chat({
                    "directive": "user_input_needed",
                    "directive_id": _make_directive_id(),
                    "reason": (
                        f"This screen has failed repeatedly and is flagged for "
                        f"manual handling. Please solve it and click Check. "
                        f"Automation will resume on the next screen."
                    ),
                    "screen_type": screen_type_hint,
                    "answer_for_user": "manual",
                    "consultation_id": consultation_id,
                }, platform, [
                    build_status(f"Manual action needed ({screen_type_hint})"),
                    build_question(
                        "This screen has been failing repeatedly. Please solve "
                        "manually and click Check. Automation resumes after."
                    ),
                ])
    except Exception:
        logger.exception("escalate: user_fallback override check failed (non-fatal)")

    # Only path to user: claude explicitly gave up by touching gave_up.flag.
    # Mac/Spark cannot create this flag — only the Mira-side claude session can.
    if gave_up.exists():
        from spark.tasks.classify_screen import _describe_screen
        return _with_chat({
            "directive": "user_input_needed",
            "directive_id": _make_directive_id(),
            "reason": f"Claude diagnosis exhausted: {reason}",
            "screen_type": screen_type_hint,
            "screen_description": _describe_screen(tree) if tree else "",
            "consultation_id": consultation_id,
        }, platform, [
            build_status("Claude exhausted diagnosis — need your help"),
            build_question(f"Claude diagnosis exhausted: {reason}"),
        ])

    retries = 0
    if retry_p.exists():
        try:
            retries = int(retry_p.read_text().strip())
        except (ValueError, OSError):
            retries = 0

    # Pending-external-research lock (Jesse 2026-05-19: "Mac stays suspended
    # until you respond, period."). When I dispatch Tier 2 (Perplexity DR) or
    # Tier 3 (Family fan-out), I create pending_external_research.flag in the
    # diag dir. The external response arrives minutes later via taeys-hands.
    # Without this lock, touching done.flag at dispatch time tells Mac to
    # retry → BT fails (no fix yet) → retry counter ticks → next tier
    # auto-fires → exhausts all tiers in ~10 minutes without ever waiting for
    # external responses. WITH this lock: done.flag is ignored as long as
    # pending_external_research.flag exists, so Mac stays in wait until the
    # external response is synthesized and the lock is released.
    pending_external = diag_dir / "pending_external_research.flag"
    if done.exists() and pending_external.exists():
        logger.warning(
            f"diagnose: done.flag touched but pending_external_research.flag "
            f"still present at {diag_dir} — premature release ignored. "
            f"Remove pending_external_research.flag first when external "
            f"response is synthesized into knowledge.json."
        )
        done.unlink()  # don't keep re-warning every poll
        # Fall through to standard wait directive below.

    # Claude finished diagnosing (knowledge.json updated). Clear flags + abandon
    # the stale consult so Mac's next /next_action runs the FRESH pipeline,
    # which spawns a new worker call using the updated knowledge.json.
    if done.exists():
        retries += 1
        retry_p.write_text(str(retries))
        done.unlink()
        diagnosing.unlink(missing_ok=True)

        # Per Jesse 2026-05-19: when claude-primary touches diagnosis_done,
        # the screen MUST be retried with the worker called and the new
        # operational_note injected. Step 3's failure path deletes the hash
        # mapping on BT failure, so without this re-register, the next
        # /next_action will Step 4 miss → Step 4.5 sig miss → Step 5 escalate
        # again — putting Mac in a never-ending diagnose loop instead of
        # retrying with the fix. Re-register the hash → screen_type_hint
        # so the next encounter hits Step 4B → fresh worker BT build.
        if screen_type_hint and screen_type_hint not in ("UNKNOWN", "UNKNOWN_NEEDS_DEFINITION"):
            try:
                from spark.tasks.variant_cache import register_hash
                # STRIP wrong-answer suffix before re-registering. If we re-register
                # with the suffix intact, the next /next_action Step-4 hit re-matches
                # the wrong-answer router (next_action.py:1667) and re-escalates to
                # claude-primary — Mac never receives a BT, so the RCA fix is never
                # executed. Infinite escalate→wait→escalate until the retry counter
                # exhausts tiers and dumps to the user. Post-RCA the retry MUST route
                # to the worker with a clean variant so the updated operational_notes
                # (e.g. Cmd+R reset) are applied. (Jesse 2026-05-23: loop confirmed.)
                _retry_hint = screen_type_hint
                for _m in ("_WRONG_RETRY", "_WRONG_ANSWER", "_NOT_QUITE", "_TRY_AGAIN"):
                    if _retry_hint.endswith(_m):
                        _retry_hint = _retry_hint[: -len(_m)]
                        break
                register_hash(platform, _screen_hash, _retry_hint)
                logger.info(
                    f"diagnose_done: re-registered hash {_screen_hash[:12]} → "
                    f"{_retry_hint} (suffix stripped from {screen_type_hint!r}) "
                    f"so worker is called on retry, not the wrong-answer router"
                )
            except Exception:
                logger.exception("diagnose_done re-register failed (non-fatal)")
        # Abandon the stale consult so its metadata.status flips off
        # "complete" — Mac's next request will find no active consult
        # and trigger fresh pipeline.
        if consultation_id:
            try:
                _stale_path = Path("/tmp/taey-ed-consult") / consultation_id
                _meta_p = _stale_path / "metadata.json"
                if _meta_p.exists():
                    _m = json.loads(_meta_p.read_text())
                    _m["status"] = "abandoned"
                    _m["abandoned_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
                    _m["abandoned_reason"] = "claude_diagnosis_complete_retry"
                    _meta_p.write_text(json.dumps(_m, indent=2))
                # Remove response.json so Mac status poll won't see it as complete
                _resp = _stale_path / "response.json"
                if _resp.exists():
                    _resp.unlink()
            except Exception:
                logger.exception("failed to abandon stale consult on diagnosis_done")
        logger.info(
            f"Claude diagnosis complete for {platform}_{_screen_hash[:16]} — "
            f"abandoned stale consult {consultation_id}, retry cycle {retries}"
        )
        return _with_chat({
            "directive": "wait",
            "directive_id": _make_directive_id(),
            "seconds": 3.0,
            "reason": "claude_diagnosis_complete",
            "message": "Claude diagnosis complete — retrying with updated knowledge.",
            "consultation_id": "",
        }, platform, [
            build_status("Claude diagnosis complete — retrying with updated knowledge"),
        ])

    # Tier-aware escalation per Jesse 2026-05-19 ladder
    # (2 me → 1 Perplexity → 1 Family → terminal). The notify body and packet
    # both come from spark.tasks.escalation; this helper just figures out the
    # tier from `retries`, builds the packet, and dispatches.
    from spark.tasks.escalation import (
        tier_for_attempt, build_packet, notify_body_for_tier, UNSOLVED_LOG,
    )
    tier = tier_for_attempt(retries)

    # Persist Mac log in the diag state dir BEFORE building the packet so the
    # packet's attempt-history section can pick it up.
    if bt_debug_tail and bt_debug_tail.strip():
        try:
            (diag_dir / "last_bt_debug.log").write_text(bt_debug_tail)
        except Exception:
            pass

    # Terminal tier: auto-mark unsolvable, log, return user_input_needed.
    if tier == "terminal":
        try:
            gave_up.touch()
            UNSOLVED_LOG.parent.mkdir(parents=True, exist_ok=True)
            with UNSOLVED_LOG.open("a") as fh:
                fh.write(
                    f"\n## {time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())} "
                    f"— {platform} {_screen_hash[:16]}\n"
                    f"- escalation_path: live_bt_failure\n"
                    f"- consultation_id: {consultation_id}\n"
                    f"- attempts_exhausted: {retries}\n"
                    f"- state_dir: {diag_dir}\n"
                    f"- failure_reason: {reason}\n"
                )
        except Exception:
            logger.exception("UNSOLVED.md append failed")
        try:
            from spark.tasks.notify_tmux import notify_spark_claude as _notify
            _notify(
                f"TERMINAL ESCALATION — {platform} screen_hash {_screen_hash[:16]} "
                f"marked unsolvable after 4-tier exhaustion. "
                f"Logged to {UNSOLVED_LOG}.",
                notify_type="defect",
            )
        except Exception:
            logger.exception("notify_spark_claude failed on terminal")
        from spark.tasks.classify_screen import _describe_screen
        return _with_chat({
            "directive": "user_input_needed",
            "directive_id": _make_directive_id(),
            "reason": f"Escalation ladder exhausted: {reason}",
            "screen_type": screen_type_hint,
            "screen_description": _describe_screen(tree) if tree else "",
            "consultation_id": consultation_id,
        }, platform, [
            build_status(f"Escalation ladder exhausted on {screen_type_hint}"),
            build_question(f"Cannot proceed: {reason}"),
        ])

    # Non-terminal: build rich-context packet + tier-aware notify (once per
    # stuck-screen cycle until done/gave_up).
    if not diagnosing.exists():
        diagnosing.touch()
        consult_path = Path("/tmp/taey-ed-consult") / consultation_id if consultation_id else diag_dir
        # Compose Mac BT execution log + failed BT into an attempts.jsonl-like
        # entry inside the state dir so build_packet's attempt-history section
        # surfaces them in the packet.
        try:
            attempts_path = diag_dir / "attempts.jsonl"
            attempt_record = {
                "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "classification": screen_type_hint,
                "failure_mode": reason,
                "analysis": "",
                "bt": failed_bt or {},
                "mac_log_tail": (bt_debug_tail or "")[-4000:],
            }
            with attempts_path.open("a") as fh:
                fh.write(json.dumps(attempt_record) + "\n")
        except Exception:
            logger.exception("attempts.jsonl append failed (non-fatal)")

        # Resolve operational_notes for the packet's known-notes section
        try:
            from spark.tasks.knowledge_loader import (
                load_knowledge, get_operational_notes_for_screen,
            )
            knowledge = load_knowledge(platform)
            notes_md = get_operational_notes_for_screen(knowledge, screen_type_hint)
        except Exception:
            knowledge = {}
            notes_md = ""

        try:
            packet_path = build_packet(
                platform=platform,
                screen_hash=_screen_hash,
                consult_path=consult_path,
                diag_state_dir=diag_dir,
                retry_count=retries,
                knowledge=knowledge,
                operational_notes_rendered=notes_md,
                screen_type_hint=screen_type_hint,
            )
        except Exception:
            logger.exception("build_packet failed; falling back to diag_dir path")
            packet_path = diag_dir / "(packet_build_failed)"

        body = notify_body_for_tier(
            tier=tier,
            packet_path=packet_path,
            platform=platform,
            screen_hash=_screen_hash,
            retry_count=retries,
            consult_path=consult_path,
            diag_state_dir=diag_dir,
        )
        try:
            from spark.tasks.notify_tmux import notify_spark_claude as _notify
            _notify(body, notify_type="escalation")
        except Exception:
            logger.exception("notify_spark_claude failed in escalate helper")
        logger.warning(
            f"Escalation triggered for {consultation_id} "
            f"({platform}, {screen_type_hint}, hash={_screen_hash[:16]}, "
            f"tier={tier}, retry_count={retries}, reason={reason!r})"
        )

    return _with_chat({
        "directive": "wait",
        "directive_id": _make_directive_id(),
        "seconds": 30.0,
        "reason": "claude_diagnosing",
        "message": f"Claude diagnosing this screen — Mac will retry automatically. ({reason})",
        "consultation_id": consultation_id,
    }, platform, [
        build_status("Claude diagnosing this screen — will retry automatically"),
    ])


def _maybe_claude_consult(
    request,
    platform: str,
    tree: dict,
    screen_type: str,
    user_guidance: str | None = None,
    failed_bt: dict | None = None,
    failed_bt_debug: str | None = None,
) -> dict | None:
    """
    If platform is Claude-primary, request a minimal consultation and return a
    directive (consulting / wait / need_screenshot). Returns None for
    Gemini-primary platforms so the caller proceeds normally.
    """
    if platform not in CLAUDE_PRIMARY_PLATFORMS:
        return None

    if not request.screenshot_b64:
        return {
            "directive": "need_screenshot",
            "directive_id": _make_directive_id(),
            "reason": f"claude_consultation_{screen_type}",
        }

    parts = []
    if user_guidance:
        parts.append(user_guidance)
    if failed_bt:
        parts.append(f"PREVIOUS BT FAILED:\n{json.dumps(failed_bt, indent=2)}")
    if failed_bt_debug:
        parts.append(f"BT debug log:\n{failed_bt_debug}")
    combined = "\n\n".join(parts) if parts else None

    from spark.tasks.consultation_request import request_minimal_consultation
    consult_result = request_minimal_consultation(
        platform=platform,
        tree=tree,
        screenshot_b64=request.screenshot_b64,
        screen_type=screen_type,
        user_guidance=combined,
        relevant_kb_chunks=request.relevant_kb_chunks,
    )
    return _consultation_or_wait(consult_result)


# ── Failure tracking (prevents infinite reclassify loops) ──
# Key: "{platform}:{variant}" → {"count": int, "last_failed": float}
# Reset when a DIFFERENT variant succeeds on the same platform.
_variant_failures: dict[str, dict] = {}
MAX_VARIANT_FAILURES = 1  # ONE SHOT: after 1 failure, escalate to claude (was 2 — Jesse 2026-05-18)


def _record_variant_failure(platform: str, variant: str):
    """Record that a variant's BT failed. Used to prevent reclassify loops."""
    key = f"{platform}:{variant}"
    entry = _variant_failures.get(key, {"count": 0})
    entry["count"] += 1
    entry["last_failed"] = time.time()
    _variant_failures[key] = entry
    logger.info(f"  Failure tracker: {variant} fail_count={entry['count']}")


def _check_variant_failed(platform: str, variant: str) -> bool:
    """Check if a variant has exceeded max failures (would loop if retried)."""
    key = f"{platform}:{variant}"
    entry = _variant_failures.get(key)
    if not entry:
        return False
    # Expire failures after 30 minutes (allow retry after cooldown)
    if time.time() - entry.get("last_failed", 0) > 1800:
        del _variant_failures[key]
        return False
    return entry["count"] >= MAX_VARIANT_FAILURES


def _clear_variant_failures(platform: str):
    """Clear failure tracking for a platform (on successful screen transition)."""
    to_clear = [k for k in _variant_failures if k.startswith(f"{platform}:")]
    for k in to_clear:
        del _variant_failures[k]


def _make_directive_id() -> str:
    return f"d-{uuid.uuid4().hex[:8]}"


def _get_extract_for_type(screen_type: str, tree: dict = None,
                          screenshot_b64: str = None, platform: str = None):
    """Build extract config for signatures missing stored extract.

    Uses dedicated Gemini call if screenshot available, otherwise returns None.
    """
    from spark.tasks.classify_screen import _should_extract, build_extract_config
    if not _should_extract(screen_type):
        return None
    if screenshot_b64 and tree and platform:
        return build_extract_config(tree, screenshot_b64, platform, screen_type)
    return None


from spark.tasks.paths import FINGERPRINT_LOG_DIR


def _log_fingerprint(platform: str, variant: str, skel_hash: str, fingerprint: dict):
    """Append fingerprint entry to JSONL log for V22 learning."""
    from datetime import datetime, timezone
    FINGERPRINT_LOG_DIR.mkdir(parents=True, exist_ok=True)
    log_path = FINGERPRINT_LOG_DIR / f"{platform}.jsonl"
    entry = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "variant": variant,
        "skeleton_hash": skel_hash,
        "fingerprint": fingerprint,
    }
    with open(log_path, "a") as f:
        f.write(json.dumps(entry) + "\n")
    logger.info(f"  Fingerprint logged: variant={variant} hash={skel_hash[:12]}")


def _with_chat(response: dict, platform: str, messages: list[dict]) -> dict:
    """Attach chat_messages to a response and persist them to Redis."""
    for msg in messages:
        store_message(platform, msg)
    response["chat_messages"] = messages
    return response



def _find_submit_in_bt(bt_def: dict) -> dict:
    """Walk BT to find submit/check button click."""
    if not isinstance(bt_def, dict):
        return {}

    if bt_def.get("type") == "action" and bt_def.get("action") == "find_and_click":
        params = bt_def.get("params", {})
        target = params.get("target", "")
        if target and isinstance(target, str) and not target.startswith("$"):
            role = params.get("role", "")
            if role in ("AXButton", "") and any(
                kw in target.lower() for kw in ["check", "submit", "next", "continue", "done"]
            ):
                return {
                    "text": target,
                    "role": role or "AXButton",
                    "strategy": params.get("strategy", "mouse_click"),
                }

    for child in bt_def.get("children", []):
        result = _find_submit_in_bt(child)
        if result:
            return result

    for key in ("do", "then", "else"):
        if key in bt_def:
            result = _find_submit_in_bt(bt_def[key])
            if result:
                return result

    return {}


def learn_from_bt_result(
    platform: str,
    screen_type: str,
    bt_result: dict,
    bt_definition: dict,
    skeleton_hash: str,
):
    """
    Extract observations from BT execution and save to learned data.
    Called after every execute_tree completion (success or interesting failure).
    Non-blocking: failures logged but don't stop pipeline.
    """
    try:
        from spark.tasks.knowledge_loader import save_learned_observation

        observation = {
            "observed_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "bt_success": bt_result.get("success", False),
            "skeleton_hash": skeleton_hash,
            "variant": bt_result.get("screen_type", screen_type),
            "details": {},
        }

        submit = _find_submit_in_bt(bt_definition)
        if submit:
            observation["details"]["submit_button"] = submit

        extract = bt_result.get("extract", {})
        if extract and isinstance(extract, dict):
            text_configs = extract.get("text", [])
            containers = [tc.get("parent_contains") for tc in text_configs if tc.get("parent_contains")]
            if containers:
                observation["details"]["content_containers"] = containers

        if not bt_result.get("success"):
            observation["failure_reason"] = bt_result.get("action", "unknown")
            if "tree_unchanged" in str(observation.get("failure_reason", "")):
                return

        save_learned_observation(platform, screen_type, observation)
    except Exception as e:
        logger.warning(f"Learning failed (non-blocking): {e}")


def _store_and_return_bt(result: dict, platform: str, tree: dict, sig_hash: str,
                         course_id: str = "") -> dict:
    """Store a Gemini-built BT with the signature and return execute_tree directive."""
    variant_type = result.get("screen_type", "UNKNOWN")

    # V21: Store BT in variant cache (deterministic types only)
    from spark.tasks.variant_cache import store_variant_bt, is_non_deterministic, register_hash
    extract_config = result.get("extract")
    try:
        if not is_non_deterministic(platform, variant_type):
            store_variant_bt(platform, variant_type, result["tree"],
                             extract_config, result.get("expected_next"),
                             source="gemini_bt")
        # Register hash → variant if we have a skeleton hash
        if sig_hash:
            register_hash(platform, sig_hash, variant_type)
        logger.info(f"  Stored variant BT for {variant_type} (hash={sig_hash[:12] if sig_hash else 'none'})")
    except Exception as e:
        logger.warning(f"  Failed to store variant BT: {e}")

    bt_json = json.dumps(result["tree"], indent=2)
    logger.info(f"  Gemini BT for {variant_type}:\n{bt_json}")

    # Pre-record BT definition for learning (actual success/failure recorded in Step 2)
    learn_from_bt_result(
        platform=platform,
        screen_type=variant_type,
        bt_result={"success": True, "screen_type": variant_type,
                   "extract": result.get("extract", {})},
        bt_definition=result.get("tree", {}),
        skeleton_hash=sig_hash,
    )

    return _with_chat({
        "directive": "execute_tree",
        "directive_id": _make_directive_id(),
        "tree": result["tree"],
        "screen": variant_type,
        "skeleton_hash": sig_hash,
        "extract": result.get("extract"),
        "expected_next": result.get("expected_next", []),
        "course_id": course_id,
    }, platform, [build_status(f"Built new {variant_type} automation — executing")])


def _consultation_or_wait(consult_result: dict) -> dict:
    """Convert request_consultation result to a directive."""
    status = consult_result.get("status", "")
    if status == "user_required":
        return {
            "directive": "user_input_needed",
            "directive_id": _make_directive_id(),
            "reason": consult_result.get("message", "Automatic resolution exhausted"),
            "consultation_id": consult_result.get("consultation_id", ""),
        }
    if status == "claude_diagnosing":
        # Mira-side Claude is editing knowledge.json. Mac sleeps and re-sends
        # /next_action; once claude_diagnosis_done.flag is set, the next call
        # falls through to a fresh worker dispatch using the updated knowledge.
        return {
            "directive": "wait",
            "directive_id": _make_directive_id(),
            "seconds": 30.0,
            "reason": "claude_diagnosing",
            "message": consult_result.get(
                "message",
                "Claude diagnosing this screen variant — Mac will retry automatically.",
            ),
            "consultation_id": consult_result.get("consultation_id", ""),
        }
    return {
        "directive": "consulting",
        "directive_id": _make_directive_id(),
        "consultation_id": consult_result["consultation_id"],
        "poll_interval": 5.0,
    }


def _build_screen_directive(request, platform: str, tree: dict, screen_type: str, sig_hash: str,
                            user_guidance: str = None, course_id: str = "") -> dict:
    """
    Gemini 2.5 Pro builds a screen-specific BT by looking at the actual tree + screenshot.
    Templates are only a fallback when no screenshot is available.

    Flow:
    1. Has screenshot? → Gemini 2.5 Pro builds dynamic BT → store with signature
    2. No screenshot? → deterministic template as last resort
    3. Both fail? → user_input_needed
    """
    # Claude-primary platforms: bypass Gemini, route to consultation.
    _claude_directive = _maybe_claude_consult(
        request, platform, tree, screen_type, user_guidance=user_guidance,
    )
    if _claude_directive:
        return _claude_directive

    from spark.tasks.classify_screen import build_bt_from_tree

    # PRIMARY PATH: Gemini 2.5 Pro builds a BT specific to THIS screen
    if request.screenshot_b64:
        logger.info(f"  Building dynamic BT via Gemini 2.5 Pro for {screen_type}")
        result = build_bt_from_tree(
            tree=tree,
            screenshot_b64=request.screenshot_b64,
            platform=platform,
            screen_type=screen_type,
            user_guidance=user_guidance,
        )
        if result:
            return _store_and_return_bt(result, platform, tree, sig_hash, course_id=course_id)
        logger.error(f"  Gemini 2.5 Pro BT build failed for {screen_type} — no template fallback")

    # Gemini failed or no screenshot — escalate, don't use templates
    from spark.tasks.classify_screen import _describe_screen
    _reason = (f"Could not build behavior tree for '{screen_type}'. "
               f"Tell me what to do on this screen.")
    return _with_chat({
        "directive": "user_input_needed",
        "directive_id": _make_directive_id(),
        "reason": _reason,
        "screen_type": screen_type,
        "screen_description": _describe_screen(tree),
    }, platform, [
        build_status(f"Could not build automation for {screen_type}"),
        build_question(_reason),
    ])


def _validate_last_action(platform: str, config: dict, lr, current_tree: dict) -> dict:
    """
    Validate the result of the previous execute_tree directive.

    Checks:
    1. Did the tree hash change? (action had effect)
    2. Does the after_tree match a known screen?
    3. Same QUIZ/ASSESSMENT screen? (wrong answer detection)
    4. New screen in expected_next? (informational)
    """
    tree_changed = lr.tree_hash_before != lr.tree_hash_after

    if not tree_changed:
        return {
            "validated": False,
            "screen_transitioned": False,
            "wrong_answer": False,
            "reason": "same_screen",
        }

    # V21: Use skeleton hash to identify where we landed
    from spark.tasks.skeleton import extract_skeleton, skeleton_hash as _skel_hash
    from spark.tasks.variant_cache import lookup_by_hash

    after_tree_to_match = lr.after_tree or current_tree
    after_skel = extract_skeleton(after_tree_to_match)
    after_hash = _skel_hash(after_skel)
    hash_result = lookup_by_hash(platform, after_hash)
    new_screen = hash_result["variant"] if hash_result else None

    if not new_screen:
        logger.warning(
            f"Step 2: after_tree matches NO known variant "
            f"(hash changed {lr.tree_hash_before[:12]} → {lr.tree_hash_after[:12]}, "
            f"after_skel_hash={after_hash[:12]})"
        )

    # Wrong answer detection
    wrong_answer = False
    if new_screen and new_screen == lr.screen:
        from spark.tasks.screen_type_util import get_master_category
        screen_master = get_master_category(new_screen)
        if screen_master == "EXERCISE":
            directive_hash = lr.directive_skeleton_hash or ""
            if after_hash and directive_hash and after_hash != directive_hash:
                logger.info(
                    f"Step 2: Same variant {new_screen} but different skeleton hash "
                    f"({directive_hash[:12]} → {after_hash[:12]}). "
                    f"Progress to next question, not wrong answer."
                )
            else:
                wrong_answer = True

    # Expected_next check (informational)
    expected_next = lr.directive_expected_next or []
    expected_match = None
    if expected_next and new_screen:
        expected_match = new_screen in expected_next

    validated = tree_changed and not wrong_answer

    return {
        "validated": validated,
        "screen_transitioned": tree_changed,
        "new_screen": new_screen,
        "wrong_answer": wrong_answer,
        "expected_next_match": expected_match,
        "after_skeleton_hash": after_hash,
        "reason": "validated" if validated else ("wrong_answer" if wrong_answer else "validation_failed"),
    }


@router.post("/session/reset")
def session_reset(platform: str = "khan_academy"):
    """Mac calls this when the user hits Stop on Run Continuous.
    Clears stale spark-side state so the next session starts fresh.
    Per Jesse 2026-05-20: 'we can't be out of sync. When user hits stop,
    it has to clear everything for that user.'

    Cleared:
      - /tmp/taey-ed-claude-diagnosing/<platform>_*  (diagnosing flags, retry counters)
      - /tmp/taey-ed-consult/consult_*  (open consults — abandoned)
      - pending_validations for this platform

    Preserved:
      - hash_index (learned screen → variant mappings)
      - variant_cache (canonical BTs)
      - knowledge.json operational_notes
      - signatures (learned screen signatures)
    """
    from pathlib import Path
    import shutil
    cleared = {"diag_dirs": 0, "consults": 0, "pending_validations": 0}

    # 1. Diagnosing state dirs for this platform
    diag_root = Path("/tmp/taey-ed-claude-diagnosing")
    if diag_root.exists():
        for d in diag_root.glob(f"{platform}_*"):
            try:
                shutil.rmtree(d)
                cleared["diag_dirs"] += 1
            except Exception:
                logger.exception(f"failed to clear diag dir {d}")

    # 2. Open consults
    consult_root = Path("/tmp/taey-ed-consult")
    if consult_root.exists():
        for d in consult_root.glob("consult_*"):
            try:
                shutil.rmtree(d)
                cleared["consults"] += 1
            except Exception:
                logger.exception(f"failed to clear consult {d}")

    # 3. Pending validations for this platform
    pv_root = Path("/home/user/taey-ed-data/pending_validations") / platform
    if pv_root.exists():
        for f in pv_root.glob("*.json"):
            try:
                f.unlink()
                cleared["pending_validations"] += 1
            except Exception:
                logger.exception(f"failed to clear pending validation {f}")

    logger.warning(
        f"SESSION RESET for {platform}: cleared "
        f"{cleared['diag_dirs']} diag dirs, "
        f"{cleared['consults']} consults, "
        f"{cleared['pending_validations']} pending validations. "
        f"hash_index + variant_cache + knowledge.json PRESERVED."
    )
    return {"ok": True, "platform": platform, "cleared": cleared}


@router.post("/next_action")
def next_action(request: NextActionRequest):
    """Production wrapper: intercept any user_input_needed at the endpoint
    boundary and route through claude_diagnosing instead. Per Jesse 2026-05-18:
    Mac app + API do not escalate to user. Everything that isn't perfectly
    handled goes to the Mira-side Claude diagnosis loop.

    The only path to a real user_input_needed directive is when a Mira-side
    Claude has explicitly created a gave_up.flag in the diagnosis state dir
    — that is handled inside _escalate_to_claude_diagnosing, not here.
    """
    # Mac-visibility debug dump (Jesse 2026-05-19): when Mac reports a BT
    # failure (or any non-success result), dump the FULL last_result as JSON
    # to /tmp/taey-ed-mac-failure-dumps/ so we can see exactly what Mac IS
    # sending — including whether bt_debug_tail is empty, missing entirely,
    # or under a different name.
    try:
        lr = request.last_result
        if lr is not None and lr.success is False:
            from pathlib import Path as _DBGP
            _dump_dir = _DBGP("/tmp/taey-ed-mac-failure-dumps")
            _dump_dir.mkdir(parents=True, exist_ok=True)
            _ts = time.strftime("%Y%m%d_%H%M%S", time.gmtime())
            _dump_path = _dump_dir / f"failure_{_ts}_{request.platform}.json"
            # Dump the full last_result, all fields, including None/empty
            _payload = {
                "timestamp_utc": _ts,
                "platform": request.platform,
                "session_id": request.session_id,
                "has_screenshot": bool(request.screenshot_b64),
                "last_result_full": lr.model_dump(mode="json"),
                "last_result_field_presence": {
                    "directive_id": lr.directive_id is not None,
                    "success": lr.success is not None,
                    "action": lr.action is not None,
                    "screen": lr.screen is not None,
                    "tree_hash_before": lr.tree_hash_before is not None,
                    "tree_hash_after": lr.tree_hash_after is not None,
                    "continue_loop": True,  # always set since it's a bool default
                    "user_response": lr.user_response is not None,
                    "after_tree": lr.after_tree is not None,
                    "directive_skeleton_hash": lr.directive_skeleton_hash is not None,
                    "directive_expected_next": lr.directive_expected_next is not None,
                    "bt_debug_tail": lr.bt_debug_tail is not None,
                    "failed_bt": lr.failed_bt is not None,
                },
                "bt_debug_tail_len": len(lr.bt_debug_tail) if lr.bt_debug_tail else 0,
            }
            _dump_path.write_text(json.dumps(_payload, indent=2, default=str))
            logger.warning(
                f"MAC FAILURE DUMP saved to {_dump_path} "
                f"(bt_debug_tail field present: {lr.bt_debug_tail is not None}, "
                f"len: {len(lr.bt_debug_tail) if lr.bt_debug_tail else 0}; "
                f"failed_bt present: {lr.failed_bt is not None})"
            )
    except Exception:
        logger.exception("Mac-visibility dump failed (non-fatal)")

    # PRE-PIPELINE CHECK: GLOBAL "waiting on central guidance" lock.
    #
    # Per Jesse 2026-05-18: any time the system is waiting on central feedback
    # — for ANY consultation or diagnosis cycle, on ANY platform — Mac should
    # do nothing but wait. The previous per-(platform, hash) check leaked when
    # Mac's tree mutated mid-diagnosis (animation, page auto-advance) and the
    # new hash escaped the lock, cascading into fresh consultations.
    #
    # "Waiting on central guidance" = either of:
    #   1. An open consultation: /tmp/taey-ed-consult/consult_*/ with no
    #      response.json yet (the worker is still computing or pending).
    #   2. An active diagnosis: /tmp/taey-ed-claude-diagnosing/*/ with a
    #      diagnosing.flag and no diagnosis_done.flag / gave_up.flag.
    #
    # While either condition holds anywhere, every /next_action returns wait.
    # Holds across platforms, screens, and tree mutations. Prevents cascade.
    try:
        _waiting_reasons = []

        # Condition 1: open consultations (response.json missing)
        _consult_root = Path("/tmp/taey-ed-consult")
        if _consult_root.exists():
            for _consult_dir in _consult_root.iterdir():
                if not _consult_dir.is_dir() or not _consult_dir.name.startswith("consult_"):
                    continue
                if (_consult_dir / "response.json").exists():
                    continue
                # Check metadata — abandoned/stale consults shouldn't lock us
                _meta = _consult_dir / "metadata.json"
                if _meta.exists():
                    try:
                        _m = json.loads(_meta.read_text())
                        if _m.get("status") == "abandoned":
                            continue
                    except Exception:
                        pass
                _waiting_reasons.append(f"open_consult:{_consult_dir.name}")
                if len(_waiting_reasons) >= 4:
                    break

        # Condition 2: active diagnosis cycles
        _diag_root = Path("/tmp/taey-ed-claude-diagnosing")
        if _diag_root.exists():
            for _state_dir in _diag_root.iterdir():
                if not _state_dir.is_dir():
                    continue
                if (_state_dir / "diagnosing.flag").exists() \
                        and not (_state_dir / "diagnosis_done.flag").exists() \
                        and not (_state_dir / "gave_up.flag").exists():
                    _waiting_reasons.append(f"diagnosing:{_state_dir.name}")
                    if len(_waiting_reasons) >= 4:
                        break

        if _waiting_reasons:
            logger.info(
                f"PRE-PIPELINE: {len(_waiting_reasons)} central-feedback wait(s) active "
                f"({', '.join(_waiting_reasons[:3])}"
                f"{'...' if len(_waiting_reasons) > 3 else ''}) "
                f"— returning wait, skipping pipeline"
            )
            return {
                "directive": "wait",
                "directive_id": _make_directive_id(),
                "seconds": 30.0,
                "reason": "central_feedback_pending",
                "message": (
                    f"Waiting on central guidance ({len(_waiting_reasons)} cycle(s)) — "
                    f"Mac will retry automatically."
                ),
            }
    except Exception:
        logger.exception("PRE-PIPELINE central-feedback check failed (non-fatal)")

    response = _next_action_impl(request)
    if isinstance(response, dict) and response.get("directive") == "user_input_needed":
        # Per Jesse 2026-05-19: when the implementation has KNOWN the answer
        # but can't reliably execute the BT (claude-primary-as-worker fallback),
        # let user_input_needed through so the user can finish manually. The
        # `answer_for_user` field marks this case explicitly.
        if response.get("answer_for_user"):
            logger.info(
                f"WRAPPER: user_input_needed PASSED THROUGH — answer_for_user present "
                f"(screen={response.get('screen_type')!r})"
            )
            return response

        platform = request.platform
        tree = request.tree
        # Best-effort consultation_id from client state, then any response field
        cs = request.client_state or ClientState()
        consult_id = (
            (cs.active_consultation_id or "")
            or response.get("consultation_id", "")
            or ""
        )
        reason = response.get("reason", "user_input_needed intercepted by wrapper")
        screen_type_hint = response.get("screen_type", "UNKNOWN")
        # Forward Mac BT execution log + failed BT into the diagnosis ping
        lr = request.last_result
        bt_debug_tail = (lr.bt_debug_tail or "") if lr else ""
        failed_bt = lr.failed_bt if lr and lr.failed_bt else None
        logger.warning(
            f"WRAPPER: intercepted user_input_needed → claude_diagnosing "
            f"(consult={consult_id!r}, screen={screen_type_hint!r}, "
            f"bt_log={'yes' if bt_debug_tail else 'no'}, failed_bt={'yes' if failed_bt else 'no'})"
        )
        return _escalate_to_claude_diagnosing(
            platform=platform,
            tree=tree,
            consultation_id=consult_id,
            reason=reason,
            screen_type_hint=screen_type_hint,
            bt_debug_tail=bt_debug_tail,
            failed_bt=failed_bt,
        )
    return response


def _next_action_impl(request: NextActionRequest):
    """
    Directive Model: Mac sends state, Spark returns ONE directive.

    Replaces /match + /consult + /validate + /action_review from Mac's perspective.
    """
    platform = request.platform
    tree = request.tree
    cs = request.client_state or ClientState()
    lr = request.last_result

    config = load_yaml(platform)

    # Store incoming user chat message (proactive or response)
    if request.chat_message:
        store_message(platform, build_user_message(request.chat_message))
        logger.info(f"  Chat: stored user message: {request.chat_message[:80]}")

        # ── URGENT: User message = priority override ──
        # User messages are treated like error reports — full context, immediate action.
        # If user is telling us something, there's something wrong that needs addressing.
        if request.screenshot_b64:
            logger.info("  URGENT: User sent proactive message — overriding normal flow")
            # Build context from previous state
            guidance_parts = [f"USER MESSAGE (URGENT — address immediately): {request.chat_message}"]
            if lr:
                guidance_parts.append(f"Previous screen: {lr.screen or 'unknown'}")
                guidance_parts.append(f"Previous action: {lr.action or 'unknown'}")
                guidance_parts.append(f"Action succeeded: {lr.success}")
                if lr.bt_debug_tail:
                    guidance_parts.append(f"Last BT debug:\n{lr.bt_debug_tail}")
            user_guidance = "\n".join(guidance_parts)

            # Claude-primary platforms: bypass Gemini, route to consultation.
            _claude_directive = _maybe_claude_consult(
                request, platform, tree,
                screen_type=(lr.screen if lr else "UNKNOWN") or "UNKNOWN",
                user_guidance=user_guidance,
            )
            if _claude_directive:
                return _claude_directive

            # Classify current screen to give Gemini context
            from spark.tasks.classify_screen import classify_screen, build_bt_from_tree
            classification = classify_screen(
                tree=tree,
                screenshot_b64=request.screenshot_b64,
                platform=platform,
            )
            screen_type = classification.get("screen_type", "UNKNOWN")
            logger.info(f"  URGENT: Current screen classified as {screen_type}")

            result = build_bt_from_tree(
                tree=tree,
                screenshot_b64=request.screenshot_b64,
                platform=platform,
                screen_type=screen_type,
                user_guidance=user_guidance,
            )
            if result:
                from spark.tasks.skeleton import extract_skeleton, skeleton_hash as _urgent_hash
                sig_hash = _urgent_hash(extract_skeleton(tree))
                return _store_and_return_bt(result, platform, tree, sig_hash, course_id=cs.course_id)

            # Gemini couldn't build BT — ask user for more specific guidance
            logger.warning("  URGENT: Gemini couldn't build BT from user message")
            from spark.tasks.classify_screen import _describe_screen
            _reason = (f"I received your message but couldn't build an automation from it. "
                       f"Can you tell me more specifically what to do on this screen?")
            return _with_chat({
                "directive": "user_input_needed",
                "directive_id": _make_directive_id(),
                "reason": _reason,
                "screen_type": screen_type,
                "screen_description": _describe_screen(tree),
            }, platform, [
                build_status(f"Received your message — need more specific guidance"),
                build_question(_reason),
            ])

    logger.info(
        f">>> /next_action: platform={platform} "
        f"course_id={cs.course_id} "
        f"has_screenshot={'yes' if request.screenshot_b64 else 'no'} "
        f"has_last_result={'yes' if lr else 'no'} "
        f"active_consultation={cs.active_consultation_id or 'none'}"
    )
    if lr:
        logger.info(
            f"    last_result: success={lr.success} screen={lr.screen} "
            f"action={lr.action} continue_loop={lr.continue_loop} "
            f"hash_before={lr.tree_hash_before[:12] if lr.tree_hash_before else 'none'} "
            f"hash_after={lr.tree_hash_after[:12] if lr.tree_hash_after else 'none'}"
        )
        if lr.bt_debug_tail:
            logger.info(f"    bt_debug_tail:\n{lr.bt_debug_tail}")

    # ── Step 1: Active consultation? Check if done ──
    logger.info("  Step 1: Checking active consultation...")
    consultation_id = cs.active_consultation_id
    if consultation_id:
        consult_status = check_consultation(consultation_id)
        if consult_status.get("status") == "complete":
            # Worker-fallback contract: when the consultation worker fails to
            # generate a BT, it writes a response.json with _worker_fallback=True
            # and an inert wait BT. Convert that here to a real user_input_needed
            # directive — the Mac should prompt the user, not execute the wait.
            if consult_status.get("_worker_fallback"):
                _wf_reason = consult_status.get(
                    "_worker_failure_reason", "worker failed to generate BT"
                )
                logger.error(
                    f"Step 1: worker_fallback for {consultation_id} — "
                    f"routing through claude_diagnosing helper. reason={_wf_reason}"
                )
                # Route through the single canonical helper. It checks done.flag
                # (abandons stale consult, signals retry), gave_up.flag (user
                # fallback), or notifies and returns wait if neither.
                return _escalate_to_claude_diagnosing(
                    platform=platform,
                    tree=tree,
                    consultation_id=consultation_id,
                    reason=f"worker_fallback: {_wf_reason}",
                    screen_type_hint=consult_status.get("screen_type", "UNKNOWN"),
                    bt_debug_tail=(lr.bt_debug_tail or "") if lr else "",
                    failed_bt=lr.failed_bt if lr and lr.failed_bt else None,
                )
            tree_def = consult_status.get("tree")
            if tree_def:
                # Bug #8: Get skeleton_hash from CURRENT tree, not stale consultation
                _consult_skeleton_hash = ""
                try:
                    _consult_tree_file = Path("/tmp/taey-ed-consult") / consultation_id / "tree.json"
                    if _consult_tree_file.exists():
                        from spark.tasks.skeleton import extract_skeleton, skeleton_hash as _skel_hash
                        _ax_tree = json.loads(_consult_tree_file.read_text())
                        _skel = extract_skeleton(_ax_tree)
                        _consult_skeleton_hash = _skel_hash(_skel)
                except Exception:
                    pass

                # Per Jesse 2026-05-19: worker MUST NOT define screen names.
                # If hash_index has a registered variant for the consult's
                # skeleton_hash, that's authoritative — override whatever
                # the worker put in its response.screen_type.
                _screen = consult_status.get("screen_type", "CONSULTATION")
                if _consult_skeleton_hash:
                    try:
                        from spark.tasks.variant_cache import lookup_by_hash as _lbh
                        _reg = _lbh(platform, _consult_skeleton_hash)
                        if _reg and _reg.get("variant"):
                            if _screen != _reg["variant"]:
                                logger.warning(
                                    f"directive override: worker said screen_type={_screen!r}, "
                                    f"hash_index registered variant={_reg['variant']!r}. "
                                    f"Using registered variant."
                                )
                            _screen = _reg["variant"]
                    except Exception:
                        logger.exception("directive variant override failed (non-fatal)")
                return _with_chat({
                    "directive": "execute_tree",
                    "directive_id": _make_directive_id(),
                    "tree": tree_def,
                    "screen": _screen,
                    "extract": consult_status.get("extract"),
                    "course_id": consult_status.get("course_id", cs.course_id),
                    "lesson": "",
                    "expected_next": consult_status.get("expected_next", []),
                    "skeleton_hash": _consult_skeleton_hash,
                }, platform, [build_status(f"Consultation resolved — executing {_screen} automation")])
            logger.warning(f"Consultation {consultation_id} complete but no tree, re-matching")
        elif consult_status.get("status") in ("not_found", "abandoned"):
            logger.warning(
                f"Consultation {consultation_id} {consult_status.get('status')}, "
                f"clearing and re-matching"
            )
            # Fall through to Step 4 (match screen) instead of waiting forever.
            # 'abandoned' is the state set by _escalate_to_claude_diagnosing when
            # done.flag is consumed — gets the pipeline running fresh with
            # updated knowledge.json instead of polling the stale consult.
        else:
            return {
                "directive": "wait",
                "directive_id": _make_directive_id(),
                "seconds": 5.0,
                "reason": "consulting",
            }

    # ── Step 2: Validate previous action result ──
    logger.info("  Step 2: Validating previous action...")
    if lr and lr.success and lr.after_tree and not lr.continue_loop:
        vr = _validate_last_action(platform, config, lr, tree)

        if vr["validated"]:
            if lr.directive_skeleton_hash:
                try:
                    from spark.tasks.variant_cache import (
                        mark_variant_validated, mark_hash_validated, lookup_by_hash,
                    )
                    mark_hash_validated(platform=platform, skel_hash=lr.directive_skeleton_hash)

                    # Per Jesse 2026-05-19: only claude-primary defines screen
                    # names — the worker MUST NOT introduce new variant labels.
                    # The authoritative variant for this hash is what's in
                    # hash_index (set when claude-primary registered the hash).
                    # If Mac's last_result.screen disagrees, that came from the
                    # worker's response.json and is drift — log and use the
                    # registered variant for validation accounting.
                    registered = lookup_by_hash(platform, lr.directive_skeleton_hash) or {}
                    registered_variant = registered.get("variant")
                    canonical_variant = registered_variant or lr.screen

                    if registered_variant and lr.screen and lr.screen != registered_variant:
                        logger.warning(
                            f"Step 2: classifier drift — Mac reported screen={lr.screen!r} "
                            f"but hash_index registered variant={registered_variant!r} "
                            f"for hash {lr.directive_skeleton_hash[:12]}. "
                            f"Using registered variant for validation."
                        )

                    if canonical_variant:
                        mark_variant_validated(platform=platform, variant=canonical_variant)

                    logger.info(
                        f"Step 2: Validated {canonical_variant} "
                        f"(hash={lr.directive_skeleton_hash[:12]}, "
                        f"new_screen={vr.get('new_screen')})"
                    )
                except Exception as e:
                    logger.warning(f"Step 2: mark_validated failed (non-fatal): {e}")

            # Success: clear failure tracking for this platform
            _clear_variant_failures(platform)

            # Learn from successful BT execution
            # Note: lr doesn't carry the BT definition — pass empty dict.
            # submit_button detection won't fire but screen_type + success is recorded.
            learn_from_bt_result(
                platform=platform,
                screen_type=lr.screen or "UNKNOWN",
                bt_result={"success": True, "screen_type": lr.screen},
                bt_definition={},
                skeleton_hash=lr.directive_skeleton_hash or "",
            )

            # Pending-validation notify (Jesse 2026-05-19): if the
            # just-executed directive came from an unvalidated screen map
            # (variant_cache or signature), ping claude-primary with the
            # outcome so the entry can be mark_validated / delete_screen'd.
            try:
                from spark.tasks.validation_tracker import (
                    check_pending, notify_claude_for_validation,
                    clear_pending, _summarize_tree,
                )
                pending = check_pending(platform, lr.directive_skeleton_hash or "")
                if pending:
                    notify_claude_for_validation(
                        record=pending,
                        last_result={
                            "success": lr.success,
                            "screen": lr.screen,
                            "tree_hash_before": lr.tree_hash_before,
                            "tree_hash_after": lr.tree_hash_after,
                            "bt_debug_tail": lr.bt_debug_tail or "",
                        },
                        after_tree_summary=_summarize_tree(tree),
                    )
                    # Clear so we don't re-notify on the next poll. claude-
                    # primary's response (mark_validated or delete) is the
                    # canonical resolution; the pending file is only the
                    # one-shot notification trigger.
                    clear_pending(platform, lr.directive_skeleton_hash or "")
            except Exception:
                logger.exception("Step 2 validation notify failed (non-fatal)")

        elif vr["wrong_answer"]:
            # ONE TRY ONLY: Wrong answer means the action was wrong. STOP.
            logger.error(
                f"Step 2: WRONG ANSWER for {lr.screen} — "
                f"ONE TRY ONLY — stopping. Hash={lr.directive_skeleton_hash}"
            )
            if lr.directive_skeleton_hash:
                try:
                    from spark.tasks.variant_cache import delete_hash, invalidate_variant_bt
                    delete_hash(platform=platform, skel_hash=lr.directive_skeleton_hash)
                    if lr.screen:
                        invalidate_variant_bt(platform=platform, variant=lr.screen)
                except Exception as e:
                    logger.warning(f"Step 2: delete_hash failed: {e}")
            from spark.tasks.classify_screen import _describe_screen
            _reason = (f"WRONG ANSWER on screen '{lr.screen}'. "
                       f"Tell me what the correct action is for this screen.")
            return _with_chat({
                "directive": "user_input_needed",
                "directive_id": _make_directive_id(),
                "reason": _reason,
                "screen_type": lr.screen or "UNKNOWN",
                "screen_description": _describe_screen(tree),
            }, platform, [
                build_status(f"Wrong answer detected on {lr.screen} — deleted old approach"),
                build_question(_reason),
            ])

        else:
            logger.warning(
                f"Step 2: Validation failed for {lr.screen}: {vr.get('reason')} "
                f"(new_screen={vr.get('new_screen')}, "
                f"expected_next_match={vr.get('expected_next_match')})"
            )

    # ── Step 2.5: Stuck detection ──
    # ONE TRY ONLY: If the action didn't change the screen, STOP.
    # Do NOT re-classify, do NOT create a consultation. Just stop.
    logger.info("  Step 2.5: Checking for stuck screen...")
    if lr and lr.success and not lr.continue_loop and lr.screen:
        tree_hash_changed = lr.tree_hash_before != lr.tree_hash_after
        if not tree_hash_changed:
            logger.error(
                f"STUCK: {lr.screen} unchanged after action. "
                f"ONE TRY ONLY — stopping. Hash={lr.directive_skeleton_hash or 'none'}"
            )
            # Delete the failed hash mapping so it doesn't re-match next time
            if lr.directive_skeleton_hash:
                try:
                    from spark.tasks.variant_cache import delete_hash as _del_hash, invalidate_variant_bt as _inv_bt
                    _del_hash(platform=platform, skel_hash=lr.directive_skeleton_hash)
                    if lr.screen:
                        _inv_bt(platform=platform, variant=lr.screen)
                    logger.info(f"Step 2.5: Deleted failed hash {lr.directive_skeleton_hash[:12]}")
                except Exception as e:
                    logger.warning(f"Step 2.5: delete_hash failed: {e}")
            from spark.tasks.classify_screen import _describe_screen
            _reason = (f"STUCK: Screen '{lr.screen}' unchanged after action. "
                       f"Tell me what to do on this screen.")
            return _with_chat({
                "directive": "user_input_needed",
                "directive_id": _make_directive_id(),
                "reason": _reason,
                "screen_type": lr.screen or "UNKNOWN",
                "screen_description": _describe_screen(tree),
            }, platform, [
                build_status(f"Screen unchanged after action on {lr.screen} — need your help"),
                build_question(_reason),
            ])

    # ── Step 2.7: Polling completion detection ──
    logger.info("  Step 2.7: Checking polling completion...")
    if lr and lr.continue_loop and lr.tree_hash_before and lr.tree_hash_after:
        # 2026-05-15: Deleted the unchanged-tree unconditional re-poll branch.
        # That branch (introduced 2026-05-14 in 5f5485f) had no exit condition
        # and would loop forever on Khan video screens where the SKELETON is
        # stable across video states (Pause -> Replay is a text-value change,
        # not a structural change). With this branch removed, tree-unchanged
        # falls through naturally to Step 4 exact-hash lookup, which hits the
        # cached video_poll BT during playback (cheap) and falls to Step 5
        # classification when the structure finally changes at completion.
        # See consultations/04_VIDEO_POLL_ARCHAEOLOGY.md Section D1.
        if lr.tree_hash_before != lr.tree_hash_after:
            # For VIDEO screens: tree hash ALWAYS changes during playback
            # (timestamps, progress bar). Check if video is actually done
            # by looking for video signals in the current tree.
            is_video = lr.screen and "VIDEO" in (lr.screen or "").upper()
            # TODO(V20): Consider using get_master_category() here for robustness.
            # Currently works because all video screen types contain "VIDEO".
            if is_video:
                from spark.tasks.prompt_codex import analyze_tree as _analyze
                current_tags = _analyze(tree)
                if "HAS_VIDEO" in current_tags:
                    logger.info(
                        f"Step 2.7: Video still playing (HAS_VIDEO in current tree). "
                        f"Continuing poll — NOT advancing."
                    )
                    # Return video_poll again to keep watching
                    return {
                        "directive": "execute_tree",
                        "directive_id": _make_directive_id(),
                        "tree": {
                            "type": "sequence",
                            "children": [
                                {"type": "action", "action": "video_poll"},
                            ],
                        },
                        "screen": lr.screen,
                        "course_id": cs.course_id,
                    }
                else:
                    logger.info(
                        f"Step 2.7: Video screen no longer has video signals. "
                        f"Video completed — navigating forward."
                    )

            logger.info(
                f"Content completed: tree changed after {lr.action} "
                f"(screen={lr.screen}). Building advancement BT via Gemini."
            )
            # Button names vary by platform — let Gemini read the actual tree
            if not request.screenshot_b64:
                return {
                    "directive": "need_screenshot",
                    "directive_id": _make_directive_id(),
                    "reason": "content_complete_advance",
                }
            return _build_screen_directive(
                request, platform, tree, "TRANSITION", "",
                user_guidance=(
                    "Content (video/article) just completed. Advance to the next item.\n"
                    "Find the explicit forward-advancement link in the AX tree — typically:\n"
                    "  - an AXLink whose name starts with 'Up next:' (read the FULL exact name from the tree, including the title after the colon)\n"
                    "  - OR a button labeled 'Continue', 'Next question', 'Done', 'Mark complete'\n"
                    "Use find_and_click with the EXACT name read from the tree + match_mode=exact + appropriate role (AXLink or AXButton). Pin the dynamic part (e.g., the video title after 'Up next:') verbatim — do NOT use match_mode=contains.\n"
                    "DO NOT click small course-wide breadcrumb arrows named 'Next in course' / 'Previous in course' at the top of the page — those are course-wide chrome that skip past entire units, never the right advancement target.\n"
                    "DO NOT click 'Up next for you!' algorithmic-recommendation cards — those are Khan's personalized suggestions, not linear course progression. The correct 'Up next' is the AXLink in the lower-right of the video player area.\n"
                    "If neither an 'Up next:' link nor a Continue/Next button is visible in the tree, the next lesson item in the LEFT SIDEBAR (look for the first un-checkmarked sidebar AXLink) is the fallback target."
                ),
                course_id=cs.course_id,
            )

    # ── Step 3: Previous action failed? ──
    logger.info("  Step 3: Checking for previous failure...")
    if lr and lr.success is False:
        if lr.user_response:
            # User provided guidance — build BT directly using Gemini
            logger.info(
                f"Step 3: User guidance received for {lr.screen}: "
                f"'{lr.user_response[:100]}'"
            )
            if not request.screenshot_b64:
                return {
                    "directive": "need_screenshot",
                    "directive_id": _make_directive_id(),
                    "reason": "user_guidance_needs_screenshot",
                }

            # Claude-primary platforms: route to consultation with the user guidance.
            _claude_directive = _maybe_claude_consult(
                request, platform, tree,
                screen_type=lr.screen or "UNKNOWN",
                user_guidance=lr.user_response,
            )
            if _claude_directive:
                return _claude_directive

            from spark.tasks.classify_screen import build_bt_from_tree
            result = build_bt_from_tree(
                tree=tree,
                screenshot_b64=request.screenshot_b64,
                platform=platform,
                screen_type=lr.screen or "UNKNOWN",
                user_guidance=lr.user_response,
            )
            if result:
                from spark.tasks.skeleton import extract_skeleton, skeleton_hash as _s3_hash
                sig_hash = _s3_hash(extract_skeleton(tree))
                return _store_and_return_bt(result, platform, tree, sig_hash, course_id=cs.course_id)

            # Gemini couldn't build BT from user guidance — fall back to consultation
            logger.warning("Step 3: Gemini couldn't build BT from user guidance, trying consultation")
            consult_result = request_consultation(
                platform=platform,
                tree=tree,
                screenshot_b64=request.screenshot_b64,
                context={"user_guidance": lr.user_response, "failed_screen": lr.screen},
            )
            return _consultation_or_wait(consult_result)

        # If Mac sent the failed BT, try ONE Gemini call with failure context
        if lr.failed_bt and request.screenshot_b64:
            logger.info(
                f"Step 3: BT failed for {lr.screen} — retrying with failure context"
            )

            # Claude-primary platforms: route the retry to consultation with
            # the failed BT + debug log as context.
            _claude_directive = _maybe_claude_consult(
                request, platform, tree,
                screen_type=lr.screen or "UNKNOWN",
                failed_bt=lr.failed_bt,
                failed_bt_debug=lr.bt_debug_tail,
            )
            if _claude_directive:
                return _claude_directive

            from spark.tasks.classify_screen import build_bt_from_tree
            result = build_bt_from_tree(
                tree=tree,
                screenshot_b64=request.screenshot_b64,
                platform=platform,
                screen_type=lr.screen or "UNKNOWN",
                failed_bt=lr.failed_bt,
                failed_bt_debug=lr.bt_debug_tail,
            )
            if result:
                from spark.tasks.skeleton import extract_skeleton, skeleton_hash as _s3r_hash
                retry_sig_hash = _s3r_hash(extract_skeleton(tree))
                return _store_and_return_bt(result, platform, tree, retry_sig_hash, course_id=cs.course_id)
            logger.error("Step 3: Gemini retry with failure context also failed — stopping")

        # ── Screen mismatch recovery ──
        # Record failure to prevent reclassify loops
        if lr.screen:
            _record_variant_failure(platform, lr.screen)

        # If the BT came from a hash match and it failed (including Gemini
        # retry), the hash mapping was likely wrong. Delete it.
        # BUT: check failure count BEFORE falling through to reclassify.
        if lr.directive_skeleton_hash:
            logger.info(
                f"Step 3: BT failed for hash-matched screen {lr.screen} "
                f"— deleting hash {lr.directive_skeleton_hash[:12]}"
            )
            try:
                from spark.tasks.variant_cache import delete_hash as _del_hash3, invalidate_variant_bt as _inv_bt3
                _del_hash3(platform=platform, skel_hash=lr.directive_skeleton_hash)
                if lr.screen:
                    _inv_bt3(platform=platform, variant=lr.screen)
            except Exception as e:
                logger.warning(f"Step 3: delete_hash failed: {e}")

            # Check if this variant has failed too many times — DON'T reclassify
            if lr.screen and _check_variant_failed(platform, lr.screen):
                logger.error(
                    f"Step 3: LOOP GUARD — {lr.screen} has failed {MAX_VARIANT_FAILURES}+ times. "
                    f"STOPPING to prevent infinite reclassify loop."
                )
                from spark.tasks.classify_screen import _describe_screen
                _reason = (f"Screen '{lr.screen}' has failed {MAX_VARIANT_FAILURES}+ times. "
                           f"The automation cannot handle this screen type. "
                           f"Tell me what to do.")
                return _with_chat({
                    "directive": "user_input_needed",
                    "directive_id": _make_directive_id(),
                    "reason": _reason,
                    "screen_type": lr.screen or "UNKNOWN",
                    "screen_description": _describe_screen(tree),
                }, platform, [
                    build_status(f"Repeated failure on {lr.screen} — need your help"),
                    build_question(_reason),
                ])

            # Fall through to Step 4/5 for fresh classification
        else:
            # No signature to invalidate — genuinely stuck. STOP.
            bt_diag = ""
            if lr.bt_debug_tail:
                bt_diag = f"\nBT debug log:\n{lr.bt_debug_tail}"
                logger.error(
                    f"Step 3: BT execution failed for {lr.screen}, action={lr.action}. "
                    f"Stopping.{bt_diag}"
                )
            else:
                logger.error(
                    f"Step 3: BT execution failed for {lr.screen}, action={lr.action}. "
                    f"Stopping. (no bt_debug_tail)"
                )
            from spark.tasks.classify_screen import _describe_screen
            _reason = (f"Action '{lr.action}' failed on screen '{lr.screen}'. "
                       f"Tell me what to do on this screen.")
            return _with_chat({
                "directive": "user_input_needed",
                "directive_id": _make_directive_id(),
                "reason": _reason,
                "screen_type": lr.screen or "UNKNOWN",
                "screen_description": _describe_screen(tree),
                "bt_debug_tail": lr.bt_debug_tail or "",
            }, platform, [
                build_status(f"Action failed on {lr.screen}"),
                build_question(_reason),
            ])

    # Off-platform short-circuit (Jesse 2026-05-20): when Mac drifts to a
    # non-Khan browser tab (JupyterLab, ChatGPT, etc.), the AX tree's WebArea
    # name reveals it. Don't escalate; return a long wait so Mac stops
    # generating Khan-classifier escalations on irrelevant screens until the
    # user switches back to the Khan tab. Each Jupyter UI state change
    # otherwise produces a new skel_hash → new escalation → wasted churn.
    # FIX 2026-06-01: capture_tree grabs the ENTIRE browser process — EVERY
    # window — so the tree routinely holds multiple AXWebAreas (the active Khan
    # window PLUS background windows like 'Sign in - Google Accounts', Gmail,
    # JupyterLab). The original guard grabbed ONE arbitrary AXWebArea (stack
    # pop order) and parked if it wasn't Khan — which FALSELY parked a
    # genuinely-on-Khan session whenever any non-Khan window also existed in
    # the same process. Correct predicate: off-platform ONLY when NO window is
    # Khan. (True active-window scoping must happen Mac-side: capture_tree
    # records no AXMain/AXFocused, so the server cannot identify the active
    # window from what it receives. Defect for active-window-only capture +
    # active-URL pre-send gate dispatched to claude-0.)
    # CCM commit a2c2dbd (2026-06-01) now tags every node with main/focused, so
    # the server CAN disambiguate the active window. Scope the ENTIRE tree to
    # the active window's subtree HERE — once — so every downstream consumer
    # (off-platform check, extract_skeleton's _find_web_area, classification,
    # escalation packet) operates on the page the user is actually on, not an
    # arbitrary background window. Selection rule (OBSERVED on real capture
    # 2026-06-01: focused=True can appear on MULTIPLE windows' webareas, so it
    # is not uniquely reliable; main=True was clean):
    #   1. exactly one AXWindow contains a focused element → that window
    #   2. else exactly one AXWindow has main=True → that window
    #   3. else ambiguous → leave the tree unscoped (prior behavior)
    def _ax_windows(root):
        wins, stack = [], [root]
        while stack:
            n = stack.pop()
            if isinstance(n, dict):
                if n.get("role") == "AXWindow":
                    wins.append(n)
                for c in n.get("children") or []:
                    stack.append(c)
        return wins

    def _contains_focused(node):
        stack = [node]
        while stack:
            n = stack.pop()
            if isinstance(n, dict):
                if n.get("focused") is True:
                    return True
                for c in n.get("children") or []:
                    stack.append(c)
        return False

    def _webarea_name(node):
        stack = [node]
        while stack:
            n = stack.pop()
            if isinstance(n, dict):
                if n.get("role") == "AXWebArea":
                    return (n.get("name") or "").strip()
                for c in n.get("children") or []:
                    stack.append(c)
        return ""

    try:
        if tree:
            _wins = _ax_windows(tree)
            _active = None
            if len(_wins) == 1:
                _active = _wins[0]
            elif len(_wins) > 1:
                _focused_wins = [w for w in _wins if _contains_focused(w)]
                _main_wins = [w for w in _wins if w.get("main") is True]
                if len(_focused_wins) == 1:
                    _active = _focused_wins[0]
                elif len(_main_wins) == 1:
                    _active = _main_wins[0]
                # else: ambiguous → _active stays None, don't scope
            if _active is not None:
                # Scope to the PAGE CONTENT (AXWebArea), not the whole window.
                # The AXWindow still contains the browser toolbar + bookmark bar
                # (Back/Forward/Reload/Bookmark/Extensions/bookmarks), which is
                # chrome that VARIES every capture and must never reach the
                # skeleton / signature / classifier. Filtering it out HERE, once
                # at ingestion, is the boundary filter — every downstream
                # consumer then operates on clean Khan content automatically,
                # instead of each one re-implementing (or forgetting) the scope.
                def _webarea_node(node):
                    stack = [node]
                    while stack:
                        n = stack.pop()
                        if isinstance(n, dict):
                            if n.get("role") == "AXWebArea":
                                return n
                            for c in n.get("children") or []:
                                stack.append(c)
                    return None
                _wa = _webarea_node(_active)
                _scoped = _wa if _wa is not None else _active
                _active_name = _webarea_name(_scoped)
                logger.info(
                    f"INGEST scope → page content {_active_name!r} "
                    f"({len(_wins)} window(s); chrome/toolbar/bookmarks filtered out)"
                )
                tree = _scoped  # chrome filtered at the boundary, ONCE
    except Exception:
        logger.exception("active-window scoping failed (non-fatal)")

    # Off-platform short-circuit (Jesse 2026-05-20): only the ACTIVE window's
    # page matters now. If the active window isn't Khan, the user is genuinely
    # elsewhere → long wait (no escalation churn on JupyterLab/Gmail/sign-in).
    try:
        _active_webarea = _webarea_name(tree) if tree else ""
        if _active_webarea and "Khan Academy" not in _active_webarea:
            logger.info(
                f"OFF-PLATFORM: active window is {_active_webarea!r} (not Khan) "
                f"— returning long wait."
            )
            return {
                "directive": "wait",
                "directive_id": _make_directive_id(),
                "seconds": 60.0,
                "reason": "off_platform_wait",
                "message": (
                    f"Mac's active window is '{_active_webarea}', not Khan Academy. "
                    f"Waiting 60s — switch to the Khan exercise to resume."
                ),
            }
    except Exception:
        logger.exception("off-platform check failed (non-fatal)")

    # ── Step 4: V21 Exact hash lookup ──
    from spark.tasks.skeleton import extract_skeleton, skeleton_hash as _skel_hash
    from spark.tasks.variant_cache import (
        lookup_by_hash, lookup_variant_bt, register_hash, delete_hash,
        store_variant_bt, is_non_deterministic, mark_variant_validated,
        mark_hash_validated,
    )

    skel = extract_skeleton(tree)  # web_content_only=True by default
    skel_hash = _skel_hash(skel)
    logger.info(f"  Step 4: Exact hash lookup (hash={skel_hash[:12]})")

    hash_result = lookup_by_hash(platform, skel_hash)
    if hash_result:
        variant = hash_result["variant"]
        logger.info(f"  Step 4: Hash hit → variant={variant}")

        # Wrong-answer variants route DIRECTLY to claude-primary RCA, not to
        # the worker (Jesse 2026-05-20: 'wrong answers need to go to you
        # directly, not back to LLM for retry. That isn't wired properly.').
        # A wrong-answer screen means the prior LLM attempt was wrong —
        # blindly re-running the LLM with the same context produces the same
        # wrong answer. claude-primary must do the RCA (was image context
        # sufficient? KB retrieval relevant? all options enumerated?), update
        # operational_notes to fill the gap, then release for retry.
        _wrong_answer_markers = ("_WRONG_RETRY", "_WRONG_ANSWER", "_NOT_QUITE", "_TRY_AGAIN")
        if any(variant.endswith(m) for m in _wrong_answer_markers):
            logger.warning(
                f"  Step 4: WRONG-ANSWER variant {variant} — routing directly "
                f"to claude-primary RCA (not worker). Per Jesse: full root cause "
                f"analysis required, no blind LLM retry."
            )
            return _escalate_to_claude_diagnosing(
                platform=platform,
                tree=tree,
                consultation_id="",
                reason=(
                    f"Wrong-answer state on variant {variant}. Per universal "
                    f"rule: full RCA required before retry. Analyze what the "
                    f"prior LLM attempt was missing (image context, KB chunks, "
                    f"option enumeration), update operational_notes to fill "
                    f"the gap, then release for a corrected attempt."
                ),
                screen_type_hint=variant,
                screenshot_b64=request.screenshot_b64,
            )

        # Per Jesse 2026-05-19: every screen-map use of an UNVALIDATED entry
        # must notify claude-primary for validation after Mac executes the BT.
        # Record pending now; Step 2 on next /next_action picks it up.
        if not hash_result.get("validated", False):
            try:
                from spark.tasks.validation_tracker import record_pending
                record_pending(
                    platform=platform,
                    skel_hash=skel_hash,
                    variant=variant,
                    source="variant_cache",
                    tree=tree,
                )
                logger.info(
                    f"  Step 4: recorded pending validation for unvalidated "
                    f"variant_cache entry {skel_hash[:12]} → {variant}"
                )
            except Exception:
                logger.exception("Step 4 record_pending failed (non-fatal)")

        # Deterministic resolver path (Jesse 2026-05-19): when a variant has
        # a server-side rule that picks the exact click target from the AX
        # tree, run it BEFORE checking variant_cache. The resolver returns the
        # full suffixed AXLink name (e.g. "Apply: Kinetic energy: unfamiliar"
        # or "Understand: Motion: unfamiliarUp next for you!"), which is
        # guaranteed unique-on-screen because invisible screen-reader outline
        # duplicates carry shorter unsuffixed names. No LLM, no worker, no
        # guessing.
        from spark.tasks.deterministic_resolvers import (
            resolve as resolve_deterministic,
            build_click_bt,
        )
        det_target = resolve_deterministic(variant, tree)
        if det_target:
            det_bt = build_click_bt(det_target)
            logger.info(
                f"  Step 4: DETERMINISTIC RESOLVER → {variant} target={det_target!r}"
            )
            return _with_chat({
                "directive": "execute_tree",
                "directive_id": _make_directive_id(),
                "tree": det_bt,
                "screen": variant,
                "skeleton_hash": skel_hash,
                "extract": None,
                "expected_next": [],
                "course_id": cs.course_id,
            }, platform, [build_status(f"Executing {variant} (deterministic)")])

        # For deterministic variants, try to reuse stored BT
        if not is_non_deterministic(platform, variant):
            bt_entry = lookup_variant_bt(platform, variant)
            if bt_entry and bt_entry.get("behavior_tree"):
                stored_bt = bt_entry["behavior_tree"]
                bt_json = json.dumps(stored_bt, indent=2)
                logger.info(f"  Step 4: REUSING BT for {variant} (hash={skel_hash[:12]})")
                logger.info(f"  Stored BT:\n{bt_json}")
                return _with_chat({
                    "directive": "execute_tree",
                    "directive_id": _make_directive_id(),
                    "tree": stored_bt,
                    "screen": variant,
                    "skeleton_hash": skel_hash,
                    "extract": bt_entry.get("extract"),
                    "expected_next": bt_entry.get("expected_next", []),
                    "course_id": cs.course_id,
                }, platform, [build_status(f"Executing {variant} automation")])

        # Per-hash user-fallback override (Jesse 2026-05-19): specific
        # screens that have failed repeatedly across the session can be
        # flagged for direct user handoff via a file at
        # /home/user/taey-ed-data/user_fallback_hashes.txt (one hash per line).
        # The hash is matched as a substring so prefix matches also fire.
        try:
            from pathlib import Path as _P
            _hash_override = _P("/home/user/taey-ed-data/user_fallback_hashes.txt")
            if _hash_override.exists():
                _flagged = _hash_override.read_text()
                if skel_hash in _flagged or skel_hash[:12] in _flagged:
                    logger.info(
                        f"  Step 4: hash {skel_hash[:12]} on user-fallback override list — handing to user"
                    )
                    return _with_chat({
                        "directive": "user_input_needed",
                        "directive_id": _make_directive_id(),
                        "reason": (
                            f"This screen ({variant}) has failed repeatedly. "
                            f"Please solve it manually and click Check. "
                            f"Automation will resume on the next screen."
                        ),
                        "screen_type": variant,
                        "answer_for_user": "manual",
                        "consultation_id": "",
                    }, platform, [
                        build_status(f"Manual action needed on {variant}"),
                        build_question(
                            f"This screen has been failing repeatedly. "
                            f"Please solve manually and click Check. Automation resumes after."
                        ),
                    ])
        except Exception:
            logger.exception("user_fallback_hashes check failed (non-fatal)")

        # User-fallback variants (Jesse 2026-05-19): these widget types can't
        # be reliably automated with current OS-level event injection (Khan's
        # interactive_graph uses native PointerEvents that synthetic mouse
        # events don't trigger; vision target identification is also unreliable).
        # Hand directly to the user — no automation attempt, no time wasted.
        # The user solves manually and the pipeline keeps flowing on the next
        # screen.
        from spark.tasks.deterministic_resolvers import CLAUDE_PRIMARY_WORKER_VARIANTS
        if variant in CLAUDE_PRIMARY_WORKER_VARIANTS:
            logger.info(
                f"  Step 4: {variant} is in user-fallback set — handing to user (no auto-attempt)"
            )
            return _with_chat({
                "directive": "user_input_needed",
                "directive_id": _make_directive_id(),
                "reason": (
                    f"This {variant} widget can't be reliably automated. "
                    f"Please solve it manually and click Check. The automation "
                    f"will resume on the next screen."
                ),
                "screen_type": variant,
                "answer_for_user": "manual",
                "consultation_id": "",
            }, platform, [
                build_status(f"Manual action needed on {variant}"),
                build_question(
                    f"This {variant.replace('EXERCISE_', '').replace('_', ' ').lower()} "
                    f"widget can't be reliably automated. Please plot the points "
                    f"manually and click Check. Automation will resume after."
                ),
            ])

        # Non-deterministic variant (EXERCISE) or no stored BT — need Pro
        logger.info(f"  Step 4B: Hash known as {variant} but needs fresh BT")
        validated = hash_result.get("validated", False)
        if not validated:
            logger.warning(
                f"  Step 4B: variant_cache entry for hash={skel_hash[:12]} is UNVALIDATED "
                f"(variant={variant}). claude-primary should mark_validated or "
                f"delete after observed-success on this screen."
            )
        if not request.screenshot_b64:
            return {
                "directive": "need_screenshot",
                "directive_id": _make_directive_id(),
                "reason": f"bt_build_for_{variant}",
            }
        # Pass the FULL variant string (e.g. "NAVIGATION_COURSE_OVERVIEW")
        # downstream, not just the master prefix. Was: variant.split("_")[0]
        # which dropped the subtype info needed by knowledge.json's subtype
        # matcher / operational_notes loader. Bug fix 2026-05-19.
        return _build_screen_directive(request, platform, tree, variant, skel_hash,
                                       course_id=cs.course_id)

    # ── Step 4.5: Signature-based fuzzy match (V19/V20 era — resurrected 2026-05-19) ──
    # When Step 4's exact skeleton_hash misses, try signature matching BEFORE
    # invoking the LLM classifier in Step 5. Signatures are stored per-platform
    # in /home/user/taey-ed-data/signatures/{platform}.json. Cold-start (< 2
    # signatures) falls back to exact-hash within the signature matcher. Once
    # 2+ signatures are learned, Jaccard similarity on discriminative markers
    # (signature - platform_common_chrome) at threshold 0.70 catches screens
    # with the same structural identity but mutated tree (different question
    # text, different timer values, different option strings).
    #
    # Empty signatures dir = always matched:False = pure passthrough, current
    # Step 5 behavior unchanged. Behavior becomes deterministic + zero-LLM-cost
    # only once learn_screen has populated entries (next commit seeds known
    # Khan screen types).
    from spark.tasks.screen_signatures import match_signature
    sig_result = match_signature(platform, tree)
    if sig_result.get("matched"):
        variant = sig_result["screen_type"]
        score = sig_result["match_score"]
        sig_hash = sig_result["sig_hash"]
        logger.info(
            f"  Step 4.5: SIGNATURE MATCH → variant={variant} "
            f"score={score:.2f} sig_hash={sig_hash} "
            f"validated={sig_result.get('validated', False)}"
        )
        # Populate the fast-path skeleton_hash cache so future encounters with
        # the same structural hash hit Step 4 directly (no Jaccard pass).
        register_hash(platform, skel_hash, variant)

        # Pending-validation notify hook (Jesse 2026-05-19): if the matched
        # signature is unvalidated, record so Step 2 pings claude-primary
        # after Mac's BT execution.
        if not sig_result.get("validated", False):
            try:
                from spark.tasks.validation_tracker import record_pending
                record_pending(
                    platform=platform,
                    skel_hash=skel_hash,
                    variant=variant,
                    source="signature",
                    tree=tree,
                    sig_hash=sig_hash,
                )
                logger.info(
                    f"  Step 4.5: recorded pending validation for unvalidated "
                    f"signature {sig_hash[:12]} → {variant}"
                )
            except Exception:
                logger.exception("Step 4.5 record_pending failed (non-fatal)")

        # Deterministic resolver short-circuit (Jesse 2026-05-19) — same path
        # as Step 4. When a server-side rule can pick the click target from
        # the AX tree, do it here BEFORE falling back to stored BT / fresh build.
        from spark.tasks.deterministic_resolvers import (
            resolve as resolve_deterministic,
            build_click_bt,
        )
        det_target = resolve_deterministic(variant, tree)
        if det_target:
            det_bt = build_click_bt(det_target)
            logger.info(
                f"  Step 4.5: DETERMINISTIC RESOLVER → {variant} target={det_target!r}"
            )
            return _with_chat({
                "directive": "execute_tree",
                "directive_id": _make_directive_id(),
                "tree": det_bt,
                "screen": variant,
                "skeleton_hash": skel_hash,
                "sig_hash": sig_hash,
                "extract": None,
                "course_id": cs.course_id,
            }, platform, [build_status(f"Executing {variant} (deterministic)")])

        if sig_result.get("tree"):
            stored_bt = sig_result["tree"]
            bt_json = json.dumps(stored_bt, indent=2)
            logger.info(f"  Step 4.5: REUSING signature-matched BT for {variant}")
            logger.info(f"  Stored BT:\n{bt_json}")
            return _with_chat({
                "directive": "execute_tree",
                "directive_id": _make_directive_id(),
                "tree": stored_bt,
                "screen": variant,
                "skeleton_hash": skel_hash,
                "sig_hash": sig_hash,
                "extract": sig_result.get("extract"),
                "course_id": cs.course_id,
            }, platform, [build_status(f"Executing {variant} (signature match)")])

        # Signature matched but no stored BT — route to BT-build with the
        # known screen_type. Avoids the LLM classifier call in Step 5.
        logger.info(f"  Step 4.5: Signature matched as {variant} but no stored BT — building fresh")
        if not request.screenshot_b64:
            return {
                "directive": "need_screenshot",
                "directive_id": _make_directive_id(),
                "reason": f"bt_build_for_{variant}_via_signature",
            }
        return _build_screen_directive(
            request, platform, tree, variant, skel_hash,
            course_id=cs.course_id,
        )
    elif sig_result.get("ambiguous"):
        # Per Jesse 2026-05-19: strict-match returned 2+ candidates with
        # identical discriminative markers. Escalate to claude-primary with
        # the candidate list so a discriminator can be added to tighten one
        # of them. Don't silently pick one (that's how fuzzy matching used
        # to absorb new variants into wrong existing ones).
        candidates = sig_result.get("candidates", [])
        shared = sig_result.get("shared_markers", [])
        candidate_str = ", ".join(f"{c['screen_type']}({c['sig_hash'][:8]})" for c in candidates)
        ambig_reason = (
            f"ambiguous_signature — {len(candidates)} variants share the same "
            f"discriminative markers: {candidate_str}. "
            f"Add a discriminator (button name, role-count delta) to one of "
            f"them so the next encounter matches exactly one."
        )
        logger.warning(f"  Step 4.5: AMBIGUOUS — {ambig_reason}")
        return _escalate_to_claude_diagnosing(
            platform=platform,
            tree=tree,
            consultation_id="",
            reason=ambig_reason,
            screen_type_hint="AMBIGUOUS_SIGNATURE",
            screenshot_b64=request.screenshot_b64,
        )
    else:
        logger.info(f"  Step 4.5: no signature match — falling through to Step 5")

    # ── Step 5: No hash match — knowledge gate, then Flash classify ──
    logger.info(f"  Step 5: No hash match — checking knowledge gate")

    # Knowledge gate: no knowledge.json = research required first
    from spark.tasks.knowledge_loader import load_knowledge
    knowledge = load_knowledge(platform)
    if not knowledge:
        knowledge_path = f"spark/platforms/{platform}/knowledge.json"
        logger.warning(f"  Step 5: KNOWLEDGE GATE — no knowledge.json for {platform}")

        gate_flag = Path(f"/tmp/taey-ed-knowledge-gate-{platform}")
        if not gate_flag.exists():
            from spark.tasks.notify_tmux import notify_spark_claude
            prompt_template = "spark/platforms/DEEP_RESEARCH_PROMPT.md"
            platform_url = {
                "khan_academy": "https://www.khanacademy.org",
                "coursera": "https://www.coursera.org",
                "edx": "https://www.edx.org",
                "udemy": "https://www.udemy.com",
                "acellus": "https://www.acellus.com",
            }.get(platform, "UNKNOWN — look it up")
            notify_spark_claude(
                f"RESEARCH REQUIRED: No knowledge.json exists for platform '{platform}'.\n"
                f"You MUST use Perplexity Deep Research via taey's hands MCP tools.\n"
                f"DO NOT use WebSearch, WebFetch, or any other substitute.\n"
                f"DO NOT delegate this to a subagent — subagents cannot use MCP tools.\n\n"
                f"STEPS:\n"
                f"1. Read the prompt template: {prompt_template}\n"
                f"2. Fill placeholders: PLATFORM_NAME={platform}, PLATFORM_URL={platform_url}\n"
                f"3. Send filled prompt to Perplexity (Deep Research mode)\n"
                f"4. Extract JSON from response (Download, not Copy)\n"
                f"5. Save to: {knowledge_path}\n"
                f"6. Create dir: spark/platforms/{platform}/learned/\n"
                f"7. Platform will proceed automatically on next screen cycle."
            )
            gate_flag.write_text(str(time.time()))
            logger.info(f"  Step 5: Notified Spark Claude (first time for {platform})")
        else:
            logger.info(f"  Step 5: Already notified for {platform}, returning wait")

        return {
            "directive": "wait",
            "directive_id": _make_directive_id(),
            "seconds": 30.0,
            "reason": f"Waiting for knowledge.json research for {platform}",
        }

    # Knowledge gate passed
    gate_flag = Path(f"/tmp/taey-ed-knowledge-gate-{platform}")
    if gate_flag.exists():
        gate_flag.unlink()
        logger.info(f"  Step 5: Knowledge gate cleared for {platform}")

    # Claude-primary platforms with no signature match: escalate to
    # claude-primary (me) to DEFINE this screen — write the signature entry
    # via learn_screen() so the next encounter is deterministic.
    #
    # Per Jesse 2026-05-19: UNKNOWN should go to claude-primary, not LLM
    # classifier. The discovery loop:
    #   1. New screen pattern → no Step 4 hash, no Step 4.5 signature
    #   2. Escalation packet routes to me with raw tree + screenshot
    #   3. I read it, decide screen_type from existing knowledge.json
    #      subtypes (or add a new subtype if novel), call learn_screen()
    #      with the platform's canonical bt_template for that subtype
    #      (or None for non-deterministic variants — Step 4.5 falls to
    #      build path with the known variant)
    #   4. Mac retries → Step 4.5 hits signature → executes deterministically
    # The system is "properly mapped once" per Jesse's AI-Native vision —
    # zero recurring LLM classification cost per known platform.
    if platform in CLAUDE_PRIMARY_PLATFORMS:
        if not request.screenshot_b64:
            logger.info("  Step 5: Claude-primary needs screenshot for escalation")
            return {
                "directive": "need_screenshot",
                "directive_id": _make_directive_id(),
                "reason": "claude_define_screen",
            }
        logger.info(
            "  Step 5: Claude-primary no-signature-match → "
            "escalating to claude-primary to define this screen"
        )
        return _escalate_to_claude_diagnosing(
            platform=platform,
            tree=tree,
            consultation_id="",
            reason=(
                "no_signature_match — UNKNOWN screen needs definition. "
                "Read consult tree + screenshot, determine screen_type, "
                "call learn_screen(platform, tree, screen_type, "
                "behavior_tree=<canonical pattern or None>) to persist."
            ),
            screen_type_hint="UNKNOWN_NEEDS_DEFINITION",
            bt_debug_tail="",
            failed_bt=None,
            screenshot_b64=request.screenshot_b64,
        )

    # Step 5A: Need screenshot for Flash classification
    if not request.screenshot_b64:
        logger.info("  Step 5A: Requesting screenshot for Flash classification")
        return {
            "directive": "need_screenshot",
            "directive_id": _make_directive_id(),
            "reason": "classification_needed",
        }

    # Step 5B: Screen classification via Claude CLI (Opus 4.7).
    # Per Jesse 2026-05-12: no Gemini in the codebase. The previous
    # flash_classify.py path was using the Gemini API with an empty key, which
    # silently fell back to UNKNOWN for every screen — that broke every
    # downstream subtype-aware path (operational_notes loading, variant cache,
    # learned observations). The replacement uses the existing
    # classify_screen() Claude CLI path at spark.tasks.classify_screen:100.
    from spark.tasks.classify_screen import classify_screen
    logger.info("  Step 5B: Classifying screen via Claude CLI...")
    classification = classify_screen(
        tree=tree,
        screenshot_b64=request.screenshot_b64,
        platform=platform,
    )
    screen_type = classification.get("screen_type", "UNKNOWN")
    # classify_screen returns the variant under 'platform_variant'; downstream
    # callers expect 'variant'. Fall back to master screen_type when no
    # variant was extracted (Pro is allowed to return only the master type
    # for genuinely ambiguous screens).
    variant = classification.get("platform_variant") or screen_type
    logger.info(
        f"  Step 5B: Classified type={screen_type} variant={variant} "
        f"note={classification.get('confidence_note', '')}"
    )

    # Register hash → variant for future exact lookups
    if variant != "UNKNOWN":
        register_hash(platform, skel_hash, variant)

    # LOOP GUARD: If Flash reclassifies as a variant that already failed, STOP
    if variant != "UNKNOWN" and _check_variant_failed(platform, variant):
        logger.error(
            f"  Step 5B: LOOP GUARD — Flash classified as {variant} which has failed "
            f"{MAX_VARIANT_FAILURES}+ times. STOPPING."
        )
        from spark.tasks.classify_screen import _describe_screen
        _reason = (f"Screen classified as '{variant}' which has repeatedly failed. "
                   f"Tell me what to do on this screen.")
        return _with_chat({
            "directive": "user_input_needed",
            "directive_id": _make_directive_id(),
            "reason": _reason,
            "screen_type": screen_type,
            "screen_description": _describe_screen(tree),
            "confidence_note": classification.get("confidence_note", ""),
        }, platform, [
            build_status(f"Screen '{variant}' keeps failing — need your help"),
            build_question(_reason),
        ])

    # Log fingerprint for V22 learning (non-blocking)
    try:
        from spark.tasks.skeleton import extract_content_fingerprint
        fingerprint = extract_content_fingerprint(tree)
        if fingerprint:
            _log_fingerprint(platform, variant, skel_hash, fingerprint)
    except Exception as e:
        logger.warning(f"  Step 5B: fingerprint logging failed (non-blocking): {e}")

    # Step 5C: Check variant BT cache (deterministic variants only)
    if variant != "UNKNOWN" and not is_non_deterministic(platform, variant):
        bt_entry = lookup_variant_bt(platform, variant)
        if bt_entry and bt_entry.get("behavior_tree"):
            stored_bt = bt_entry["behavior_tree"]
            bt_json = json.dumps(stored_bt, indent=2)
            logger.info(f"  Step 5C: REUSING variant BT for {variant}")
            logger.info(f"  Stored BT:\n{bt_json}")
            return _with_chat({
                "directive": "execute_tree",
                "directive_id": _make_directive_id(),
                "tree": stored_bt,
                "screen": variant,
                "skeleton_hash": skel_hash,
                "extract": bt_entry.get("extract"),
                "expected_next": bt_entry.get("expected_next", []),
                "course_id": cs.course_id,
            }, platform, [build_status(f"Executing {variant} automation")])

    # Step 5D: Pro builds BT (new variant or non-deterministic)
    logger.info(f"  Step 5D: Pro building BT for {variant}")
    store_message(platform, build_status(f"Identified screen as {variant}"))

    if screen_type == "UNKNOWN":
        # UNKNOWN — try Pro BT builder, escalate if it fails
        logger.warning("  Step 5D: UNKNOWN screen — trying Gemini Pro BT builder")
        from spark.tasks.classify_screen import build_bt_from_tree, _describe_screen
        result = build_bt_from_tree(
            tree=tree,
            screenshot_b64=request.screenshot_b64,
            platform=platform,
            screen_type="UNKNOWN",
        )
        if result:
            result_variant = result.get("screen_type", "UNKNOWN")
            logger.info(f"  Step 5D: Pro built BT for UNKNOWN → {result_variant}")
            # Store variant BT if deterministic
            if not is_non_deterministic(platform, result_variant):
                store_variant_bt(platform, result_variant, result["tree"],
                                 result.get("extract"), result.get("expected_next"))
            register_hash(platform, skel_hash, result_variant)
            return _store_and_return_bt(result, platform, tree, skel_hash, course_id=cs.course_id)

        # Pro failed — escalate
        logger.warning("  Step 5D: Pro BT builder failed — requesting user input")
        _reason = ("Unknown screen type. Gemini could not build a behavior tree. "
                   "Tell me what to do on this screen.")
        return _with_chat({
            "directive": "user_input_needed",
            "directive_id": _make_directive_id(),
            "reason": _reason,
            "screen_type": "UNKNOWN",
            "screen_description": _describe_screen(tree),
            "confidence_note": classification.get("confidence_note", ""),
        }, platform, [
            build_status("Unknown screen — could not build automation"),
            build_question(_reason),
        ])

    # Known type — build BT via Pro, store for variant
    result = _build_screen_directive(request, platform, tree, screen_type, skel_hash,
                                     course_id=cs.course_id)

    # If Pro built a BT successfully, store it under the variant for reuse
    if result.get("directive") == "execute_tree" and not is_non_deterministic(platform, variant):
        bt = result.get("tree")
        if bt:
            store_variant_bt(platform, variant, bt,
                             result.get("extract"), result.get("expected_next"))

    return result
