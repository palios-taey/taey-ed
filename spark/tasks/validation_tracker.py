"""
Pending-validation tracker for unvalidated screen signatures / variant_cache entries.

When Step 4 or Step 4.5 matches an UNVALIDATED entry, we record it as pending.
When Mac next reports back via last_result, the validator inspects the outcome
and pings claude-primary via taey-notify to decide: mark_validated or delete.

Per Jesse 2026-05-19: "every time you update a screen map, it should be
flagged as unvalidated and as soon as it is used, it needs to be validated
by you for now until we can figure out something automated."

Storage: /home/user/taey-ed-data/pending_validations/{platform}/{skel_hash}.json
  {
    "platform":     str,
    "skel_hash":    str (the variant_cache key)
    "sig_hash":     str | null (the signature store key, if Step 4.5 matched)
    "variant":      str (e.g. NAVIGATION_COURSE_OVERVIEW)
    "source":       "variant_cache" | "signature"
    "matched_at":   ISO timestamp
    "tree_summary": brief AX role counts for the matched tree (helps claude-primary
                    recall what screen this was without re-reading the full tree)
  }

The file is deleted once claude-primary validates (or deletes) the entry,
or when 1 hour elapses without resolution (TTL cleanup, optional).
"""
from __future__ import annotations

import json
import logging
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

PENDING_DIR = Path("/home/user/taey-ed-data/pending_validations")
TAEY_NOTIFY_BIN = "/usr/local/bin/taey-notify"


def _summarize_tree(tree: dict) -> dict:
    """Compact AX role counts so claude-primary can recognize the screen
    without re-reading the full tree.json from the consult dir."""
    if not tree:
        return {}
    counts: dict[str, int] = {}
    interactive_names: list[str] = []
    stack = [tree]
    while stack:
        n = stack.pop()
        r = n.get("role", "")
        if r:
            counts[r] = counts.get(r, 0) + 1
        if r in ("AXButton", "AXLink"):
            nm = (n.get("name") or "").strip()
            if nm and not nm.startswith("Unnamed bookmark") and len(interactive_names) < 20:
                bbox = n.get("visible_bbox") or [0, 0, 0, 0]
                if bbox[1] > 200:  # below browser chrome
                    interactive_names.append(f"{r}: {nm[:60]!r}")
        for c in n.get("children", []) or []:
            stack.append(c)
    # WebArea name = page title — most discriminative single signal
    webarea_name = ""
    stack = [tree]
    while stack:
        n = stack.pop()
        if n.get("role") == "AXWebArea":
            webarea_name = (n.get("name") or "").strip()
            break
        for c in n.get("children", []) or []:
            stack.append(c)
    return {
        "webarea_name": webarea_name,
        "role_counts": {r: c for r, c in sorted(counts.items(), key=lambda kv: -kv[1])[:10]},
        "interactive_samples": interactive_names[:15],
    }


def record_pending(
    *,
    platform: str,
    skel_hash: str,
    variant: str,
    source: str,
    tree: dict | None = None,
    sig_hash: Optional[str] = None,
) -> None:
    """Record that an unvalidated entry was matched + used.

    Idempotent: if a pending file already exists for this (platform, skel_hash),
    we overwrite with the latest match (refreshing the timestamp). Mac may
    encounter the same screen multiple times during a Run Continuous burst
    before claude-primary responds.
    """
    pdir = PENDING_DIR / platform
    pdir.mkdir(parents=True, exist_ok=True)
    rec = {
        "platform": platform,
        "skel_hash": skel_hash,
        "sig_hash": sig_hash,
        "variant": variant,
        "source": source,
        "matched_at": datetime.now(timezone.utc).isoformat(),
        "tree_summary": _summarize_tree(tree) if tree else {},
    }
    (pdir / f"{skel_hash}.json").write_text(json.dumps(rec, indent=2))


def check_pending(platform: str, skel_hash: str) -> Optional[dict]:
    """Return the pending record for this (platform, skel_hash) or None."""
    p = PENDING_DIR / platform / f"{skel_hash}.json"
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text())
    except Exception:
        return None


def clear_pending(platform: str, skel_hash: str) -> None:
    """Remove the pending record once claude-primary has validated/deleted."""
    p = PENDING_DIR / platform / f"{skel_hash}.json"
    if p.exists():
        try:
            p.unlink()
        except Exception:
            pass


def notify_claude_for_validation(
    *,
    record: dict,
    last_result: dict,
    after_tree_summary: dict,
) -> None:
    """Send claude-primary a taey-notify message asking to validate or delete
    the just-used unvalidated entry.

    Includes the pre/post tree summaries so claude can decide without re-reading
    consult artifacts.
    """
    skel = record.get("skel_hash", "?")
    sig = record.get("sig_hash") or "(variant_cache only — no signature)"
    variant = record.get("variant", "?")
    source = record.get("source", "?")
    pre_summary = record.get("tree_summary", {})
    success = last_result.get("success")
    screen_after = last_result.get("screen", "?")
    hash_before = last_result.get("tree_hash_before", "?")
    hash_after = last_result.get("tree_hash_after", "?")
    tree_changed = hash_before != hash_after
    bt_log = last_result.get("bt_debug_tail", "")[-1500:]

    msg = (
        f"VALIDATION REQUEST — unvalidated screen map was used and BT just executed\n"
        f"\n"
        f"PLATFORM: {record.get('platform')}\n"
        f"SOURCE: {source} (signature store / variant_cache)\n"
        f"VARIANT: {variant}\n"
        f"SKEL_HASH: {skel}\n"
        f"SIG_HASH: {sig}\n"
        f"\n"
        f"PRE-EXECUTION SCREEN (the screen the map matched):\n"
        f"  WebArea: {pre_summary.get('webarea_name', '?')!r}\n"
        f"  Top roles: {pre_summary.get('role_counts', {})}\n"
        f"  Sample buttons/links:\n"
        + "\n".join(f"    - {s}" for s in pre_summary.get("interactive_samples", [])[:10]) + "\n"
        f"\n"
        f"BT EXECUTION OUTCOME (from Mac's last_result):\n"
        f"  success: {success}\n"
        f"  screen reported: {screen_after}\n"
        f"  tree_hash_before: {hash_before}\n"
        f"  tree_hash_after:  {hash_after}\n"
        f"  tree changed:     {tree_changed}\n"
        f"\n"
        f"POST-EXECUTION SCREEN (where Mac landed after the BT ran):\n"
        f"  WebArea: {after_tree_summary.get('webarea_name', '?')!r}\n"
        f"  Top roles: {after_tree_summary.get('role_counts', {})}\n"
        f"\n"
        f"MAC BT LOG (tail):\n```\n{bt_log}\n```\n"
        f"\n"
        f"DECISION REQUIRED — answer ONE of:\n"
        f"  A) The variant + BT were correct for this screen and the outcome\n"
        f"     looks right → call mark_validated('{record.get('platform')}', "
        f"'{sig}') AND set variant_cache validated=True for {skel}\n"
        f"  B) The variant was wrong OR the BT failed/advanced incorrectly →\n"
        f"     call delete_screen('{record.get('platform')}', '{sig}') AND\n"
        f"     delete_hash('{record.get('platform')}', '{skel}'). Then on the\n"
        f"     next escalation re-learn with the correct screen_type.\n"
        f"\n"
        f"After deciding, clear the pending file:\n"
        f"  clear_pending('{record.get('platform')}', '{skel}')\n"
    )
    try:
        subprocess.Popen(
            [TAEY_NOTIFY_BIN, "taey-ed-operator", "--type", "task", "--from", "spark", msg],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        logger.info(
            f"validation_tracker: notified claude-primary for "
            f"{record.get('platform')}/{skel[:12]} variant={variant}"
        )
    except Exception:
        logger.exception("validation_tracker: notify failed")
