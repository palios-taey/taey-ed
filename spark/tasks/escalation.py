"""
Escalation ladder for taey-ed consultation failures.

Ladder (per Jesse 2026-05-19, FINAL):
    Attempts 1-2  -> Tier 1: claude-primary edits knowledge.json operational_note
    Attempt  3    -> Tier 2: Perplexity DR via taeys-hands
    Attempts 4-5  -> Tier 3: Full Family fan-out (TWO loops)
    Attempt  6+   -> Terminal: gave_up.flag + UNSOLVED.md entry

    5 attempts total before terminal. Family gets two loops because the
    first loop's responses (now in the packet as prior research) inform
    the second loop's diagnoses.

Architecture: this module owns the packet builder + tier resolver. The actual
trigger lives in consultation_request.py — when retry_count hits a tier
boundary, it calls build_packet() and emits a notification pointing at the
packet path and the appropriate template file.

Templates live at /home/user/taey-ed/spark/escalation_templates/. The
notification tells the recipient (claude-primary, taeys-hands, or terminal)
which template to follow. No protocol knowledge lives in CLAUDE.md or memory —
the system reminds itself at trigger time.
"""

from __future__ import annotations

import json
import logging
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

ESCALATIONS_DIR = Path("/home/user/taey-ed/consultations/ESCALATIONS")
TEMPLATES_DIR = Path("/home/user/taey-ed/spark/escalation_templates")
UNSOLVED_LOG = Path("/home/user/taey-ed/consultations/UNSOLVED.md")


# --- Tier resolution ---------------------------------------------------------

def tier_for_attempt(retry_count: int) -> str:
    """Resolve which tier an attempt belongs to.

    retry_count is the number of completed diagnosis cycles, i.e. how many
    times claude-primary has been asked to edit knowledge.json for this
    (platform, screen_hash). 0 means this is the FIRST diagnosis request.

    Ladder per Jesse 2026-05-19 FINAL (5 attempts total):
      retry_count 0  -> tier1 (claude-primary, attempt 1)
      retry_count 1  -> tier1 (claude-primary, attempt 2)
      retry_count 2  -> tier2 (Perplexity DR, attempt 3)
      retry_count 3  -> tier3 (full Family loop 1, attempt 4)
      retry_count 4  -> tier3 (full Family loop 2, attempt 5)
      retry_count 5+ -> terminal (mark unsolvable)
    """
    if retry_count <= 1:
        return "tier1"
    if retry_count == 2:
        return "tier2"
    if retry_count in (3, 4):
        return "tier3"
    return "terminal"


def family_loop_for_tier3(retry_count: int) -> int:
    """Return 1 or 2 for the Tier 3 loop number. Only valid when tier=tier3."""
    if retry_count == 3:
        return 1
    if retry_count == 4:
        return 2
    raise ValueError(f"family_loop_for_tier3 called with retry_count={retry_count}")


def template_path_for_tier(tier: str) -> Path:
    """Map tier -> template file path."""
    mapping = {
        "tier2": TEMPLATES_DIR / "tier2_perplexity_dr.md",
        "tier3": TEMPLATES_DIR / "tier3_full_family.md",
        "terminal": TEMPLATES_DIR / "terminal_giveup.md",
    }
    return mapping.get(tier, TEMPLATES_DIR / "tier1_spark_primary.md")


# --- Packet builder ----------------------------------------------------------

def _ax_summary(tree: dict) -> str:
    """Compact AX-tree summary: role counts + top interactive elements."""
    role_counts: dict[str, int] = {}
    interactive: list[tuple[str, str, list]] = []

    interactive_roles = {
        "AXButton", "AXLink", "AXTextField", "AXTextArea",
        "AXComboBox", "AXPopUpButton", "AXCheckBox", "AXRadioButton",
        "AXList", "AXMenuItem",
    }

    def walk(node: dict) -> None:
        role = node.get("role") or ""
        if not role:
            for c in node.get("children", []) or []:
                walk(c)
            return
        if "Menu" in role and role != "AXMenuItem":
            return
        role_counts[role] = role_counts.get(role, 0) + 1
        if role in interactive_roles:
            name = (node.get("name") or "").strip()[:80]
            bbox = node.get("visible_bbox")
            if name or bbox:
                interactive.append((role, name, bbox))
        for c in node.get("children", []) or []:
            walk(c)

    try:
        walk(tree)
    except Exception as e:
        return f"(ax_summary error: {e})"

    role_lines = [f"  {r}: {c}" for r, c in sorted(role_counts.items(), key=lambda kv: -kv[1])[:15]]
    interactive_lines = [
        f"  {role:18s} name={name!r:40s} bbox={bbox}"
        for role, name, bbox in interactive[:25]
    ]
    return (
        "Role counts (top 15):\n" + "\n".join(role_lines)
        + "\n\nTop 25 interactive elements:\n"
        + ("\n".join(interactive_lines) if interactive_lines else "  (none found)")
    )


def _read_attempt_history(diag_state_dir: Path) -> str:
    """Read attempts.jsonl + last_bt_debug.log if present.

    attempts.jsonl is a future structured log; last_bt_debug.log is the
    current state. We render whichever exists.
    """
    sections = []
    attempts_path = diag_state_dir / "attempts.jsonl"
    if attempts_path.exists():
        try:
            for i, line in enumerate(attempts_path.read_text().splitlines()):
                if not line.strip():
                    continue
                rec = json.loads(line)
                sections.append(
                    f"### Attempt {i+1} — {rec.get('timestamp', '?')}\n"
                    f"- classification: {rec.get('classification', '?')}\n"
                    f"- failure_mode: {rec.get('failure_mode', '?')}\n"
                    f"- analysis: {rec.get('analysis', '?')}\n\n"
                    f"BT:\n```json\n{json.dumps(rec.get('bt', {}), indent=2)}\n```\n"
                )
        except Exception as e:
            sections.append(f"(attempts.jsonl parse error: {e})")

    bt_log = diag_state_dir / "last_bt_debug.log"
    if bt_log.exists():
        try:
            tail = bt_log.read_text().splitlines()[-40:]
            sections.append(
                "### Mac BT execution log (last 40 lines)\n```\n"
                + "\n".join(tail) + "\n```"
            )
        except Exception as e:
            sections.append(f"(last_bt_debug.log read error: {e})")

    return "\n\n".join(sections) if sections else "(no attempt history captured yet)"


def _system_capabilities_snapshot() -> str:
    """Return the canonical capabilities text injected into every packet.

    Loaded from prompt_codex.py module so it stays in sync with what the worker
    sees. Kept short — the packet is for outside agents; deep handler docs
    bloat the document.
    """
    handlers = (
        "find_and_click, find_and_type, find_all, click, click_at, drag, "
        "type_keys, extract_question, send_to_llm, video_poll, wait, "
        "press_key, scroll, wait_for_element, discover_menu, lookup_match, "
        "store_qa, solve_assessment_page, press_escape, select_dropdown_option, "
        "store_to_current"
    )
    return (
        "### BT engine primitives\n"
        "- `sequence` — children run in order, fail-fast\n"
        "- `fallback` — try children until one succeeds (NOT allowed in API responses)\n"
        "- `action` — invoke a registered handler\n"
        "- `for_each` — iterate over a list; params (items / variable / do) at TOP LEVEL not in params\n"
        "- `conditional` — params (condition / then / else) at TOP LEVEL\n"
        "- Variable resolution: `$var`, `$var.field`, `$var.0.element` (digit indexing)\n"
        "- Drag schema: `{start: {x,y}, end: {x,y}, post_delay}` — NESTED dicts only\n"
        "\n"
        "### Registered handlers (25 total)\n"
        f"{handlers}\n"
        "\n"
        "### send_to_llm question types\n"
        "solve_choice, solve_checkbox, solve, solve_matching, "
        "solve_assessment, solve_complex, navigate\n"
        "\n"
        "### Cardinal Rules\n"
        "1. Gemini/Claude is the compiler — synthesize BTs at generation time, never punt to runtime\n"
        "2. No thresholds — `analyze_tree()` detects PRESENCE only, classification is the LLM's job\n"
        "3. ONE TRY ONLY — stuck or wrong answer = stop and escalate\n"
        "4. NEVER click 'Skip' mid-exercise; 'Up next' only on completion/transition screens\n"
        "5. video_poll must be the ONLY child in its sequence\n"
        "6. for_each/conditional params at TOP LEVEL, not in `params:`\n"
        "7. No `fallback` nodes in API responses (HTTP 400)\n"
        "8. Deterministic BTs (VIDEO/ARTICLE) are reused forever after first build\n"
        "9. Dynamic BTs (EXERCISE/NAVIGATION/TRANSITION) rebuilt fresh each encounter\n"
        "10. Use `mouse_click` strategy for browser elements — `ax_press` silently fails on Chrome\n"
    )


def build_packet(
    *,
    platform: str,
    screen_hash: str,
    consult_path: Path,
    diag_state_dir: Path,
    retry_count: int,
    knowledge: dict,
    operational_notes_rendered: str,
    screen_type_hint: str = "UNKNOWN",
    prior_research: Optional[str] = None,
    specific_ask: Optional[str] = None,
) -> Path:
    """Build the rich-context escalation packet and return its path.

    Layout on disk:
        consultations/ESCALATIONS/ESC_<platform>_<hash16>_<utc>/
            packet.md
            screenshot.png   (copied from consult_path)
            tree.json        (copied from consult_path)
    """
    tier = tier_for_attempt(retry_count)
    utc = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    esc_id = f"ESC_{platform}_{screen_hash[:16]}_{utc}"
    esc_dir = ESCALATIONS_DIR / esc_id
    esc_dir.mkdir(parents=True, exist_ok=True)

    # Copy artifacts alongside the packet so it's a single-folder unit.
    # Primary source: the consult_path (worker-mode escalations have full
    # consult dir with tree.json + screenshot.png + metadata.json).
    # Fallback source: the diag_state_dir (Step 4.5 → claude-primary
    # escalations have no consult_id; tree+screenshot are persisted to the
    # diag dir at escalation time — see next_action.py::_escalate_to_
    # claude_diagnosing 2026-05-19 bug fix).
    tree_dst = esc_dir / "tree.json"
    shot_dst = esc_dir / "screenshot.png"

    def _try_copy(src_dir: Path):
        if (src_dir / "tree.json").exists() and not tree_dst.exists():
            shutil.copy2(src_dir / "tree.json", tree_dst)
        if (src_dir / "screenshot.png").exists() and not shot_dst.exists():
            shutil.copy2(src_dir / "screenshot.png", shot_dst)

    _try_copy(consult_path)
    if not tree_dst.exists() or not shot_dst.exists():
        _try_copy(diag_state_dir)

    # Load tree for AX summary
    tree = {}
    if tree_dst.exists():
        try:
            tree = json.loads(tree_dst.read_text())
        except Exception as e:
            logger.warning(f"escalation: tree.json parse failed: {e}")

    # Family loop annotation for Tier 3 (two loops per Jesse 2026-05-19 final)
    family_loop_note = ""
    if tier == "tier3":
        loop = family_loop_for_tier3(retry_count)
        family_loop_note = f"\n- tier3_loop: {loop} of 2"

    if specific_ask is None:
        specific_ask = (
            f"Produce a working BT for this {platform} screen "
            f"(variant: {screen_type_hint}). Prior attempts have failed in the "
            f"manner shown in section 5. Use the system capabilities in section 3 — "
            f"do NOT propose changes that would require new handlers unless the "
            f"existing 25-handler set is genuinely insufficient (in which case "
            f"say so and propose the minimal handler addition)."
        )

    packet = f"""# Escalation Packet — {esc_id}

**Tier**: {tier.upper()}{family_loop_note}
**Generated**: {datetime.now(timezone.utc).isoformat()}

---

## 1. Identity
- escalation_id: `{esc_id}`
- platform: `{platform}`
- screen_hash: `{screen_hash}`
- variant_hint: `{screen_type_hint}`
- attempts_so_far: {retry_count}
- created_by: claude-primary (taey-ed spark)

## 2. Screen artifacts
- screenshot: `{shot_dst}`  (attach to LLM prompt)
- tree.json: `{tree_dst}`  (full AX tree, large file — excerpt below)

### AX summary

{_ax_summary(tree)}

## 3. System capabilities (what we have to work with)

{_system_capabilities_snapshot()}

## 4. Known operational_notes for this screen type

The worker is given these notes from `knowledge.json` at BT-generation time
(platform / category / matched subtype tiers). If empty, no matched notes
exist yet — that itself is signal.

```markdown
{operational_notes_rendered or '(no matched operational_notes for this variant)'}
```

## 5. Attempt history

{_read_attempt_history(diag_state_dir)}

## 6. Prior research consulted

{prior_research or '(none yet at this tier — Tier 2+ may have Perplexity DR responses appended)'}

## 7. Specific ask

{specific_ask}

## 8. Suggested response structure (prose acceptable, no JSON requirement)

```
## Diagnosis
(what's going wrong and why, source-cited where possible)

## Proposed BT
(one or more JSON blocks the worker can adopt; or 'no viable BT — recommend handler change X')

## Confidence
(LOW / MED / HIGH and why)

## Open Questions
(anything that needs follow-up or that you couldn't answer)
```

---

*This packet is the canonical context document for this escalation. Templates
at /home/user/taey-ed/spark/escalation_templates/ tell the recipient what to do
with it — packet content stays constant; template tells the dispatch protocol.*
"""

    packet_path = esc_dir / "packet.md"
    packet_path.write_text(packet)
    logger.info(f"escalation: built packet at {packet_path} (tier={tier})")
    return packet_path


# --- Notification body builders ---------------------------------------------

def notify_body_for_tier(
    *,
    tier: str,
    packet_path: Path,
    platform: str,
    screen_hash: str,
    retry_count: int,
    consult_path: Path,
    diag_state_dir: Path,
) -> str:
    """Build the notification body for a tier escalation.

    The notification points at the packet (rich context) and the template
    (what to do with it). The recipient — claude-primary in the taey-ed
    session — reads the template and follows it.
    """
    template = template_path_for_tier(tier)
    base = (
        f"ESCALATION TIER {tier.upper()} — production loop\n"
        f"Platform: {platform}\n"
        f"Screen hash: {screen_hash}\n"
        f"Attempts so far: {retry_count}\n"
        f"State dir: {diag_state_dir}\n"
        f"Consult: {consult_path}\n"
        f"\n"
        f"PACKET (full context, read first):\n"
        f"  {packet_path}\n"
        f"\n"
        f"TEMPLATE (what to do — follow this verbatim):\n"
        f"  {template}\n"
    )
    if tier == "tier1":
        return base + (
            "\n"
            "ACTION: Edit /home/user/taey-ed/spark/platforms/{platform}/knowledge.json\n"
            "under screen_types.<master>.subtypes.<matched_variant>.operational_notes[].\n"
            "Rule must be generalizable. Touch diagnosis_done.flag when done.\n"
        )
    if tier == "tier2":
        return base + (
            "\n"
            "ACTION: Dispatch packet to Perplexity DR via taeys-hands per template.\n"
            "Remind taeys-hands to prepend FAMILY_KERNEL.md + IDENTITY_CLARITY.md.\n"
            "Touch diagnosis_done.flag after dispatch — DO NOT touch gave_up.flag.\n"
        )
    if tier == "tier3":
        loop = family_loop_for_tier3(retry_count)
        return base + (
            f"\n"
            f"ACTION: Tier 3 Loop {loop} of 2 — fan out to all 5 Family platforms via taeys-hands.\n"
            f"Remind taeys-hands to prepend FAMILY_KERNEL.md + per-platform IDENTITY_<codename>.md.\n"
            f"Touch diagnosis_done.flag after dispatch — DO NOT touch gave_up.flag.\n"
            f"If loop {loop} fails: next escalation auto-triggers "
            f"{'tier3 loop 2' if loop == 1 else 'terminal'}.\n"
        )
    # terminal
    return base + (
        "\n"
        "ACTION: Mark unsolvable. Append entry to /home/user/taey-ed/consultations/UNSOLVED.md.\n"
        "Touch gave_up.flag. Send a summary defect notification.\n"
        "Follow terminal_giveup.md exactly.\n"
    )
