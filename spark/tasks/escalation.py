"""
Escalation ladder for taey-ed consultation failures.

Ladder (per Jesse 2026-06-11 canonical flow):
    Attempts 1-2  -> Tier 1: claude-primary edits knowledge.json operational_note
    Attempt  3    -> Tier 2: Perplexity DR via taeys-hands
    Attempt  4    -> Tier 3: Full Family fan-out (ONE round)
    Attempt  5+   -> Terminal: gave_up.flag + UNSOLVED.md entry

    4 attempts total before terminal.

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

    Ladder per Jesse 2026-06-11 canonical flow (4 attempts total):
      retry_count 0  -> tier1 (claude-primary, attempt 1)
      retry_count 1  -> tier1 (claude-primary, attempt 2)
      retry_count 2  -> tier2 (Perplexity DR, attempt 3)
      retry_count 3  -> tier3 (full Family round, attempt 4)
      retry_count 4+ -> terminal (mark unsolvable)
    """
    if retry_count <= 1:
        return "tier1"
    if retry_count == 2:
        return "tier2"
    if retry_count == 3:
        return "tier3"
    return "terminal"


def template_path_for_tier(tier: str) -> Path:
    """Map tier -> template file path."""
    mapping = {
        "tier2": TEMPLATES_DIR / "tier2_perplexity_dr.md",
        "tier3": TEMPLATES_DIR / "tier3_full_family.md",
        "terminal": TEMPLATES_DIR / "terminal_giveup.md",
    }
    return mapping.get(tier, TEMPLATES_DIR / "tier1_spark_primary.md")


# --- Fleet dispatch (auto-climb) ----------------------------------------------

def notify_fleet(target: str, message: str, notify_type: str = "task") -> bool:
    """Fire-and-forget taey-notify to any fleet target.

    Auto-climb (INTENDED_FLOW §D, Jesse 2026-06-11): the SERVER dispatches
    Tier 2/3 to taeys-hands directly — claude-primary is no longer a relay.
    Mirrors notify_tmux.notify_spark_claude (that module stays frozen and
    taey-ed-targeted; this is the general-target sibling).
    """
    import subprocess
    try:
        subprocess.Popen(
            [
                "/usr/local/bin/taey-notify",
                target,
                "--type", notify_type,
                "--from", "spark",
                message,
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        return True
    except Exception as e:
        logger.error(f"notify_fleet({target}) failed: {e}")
        return False


def classify_failure_environment(bt_debug_tail) -> Optional[str]:
    """Detect environmental failure classes from the Mac BT debug tail.

    Per taeys-hands contract (2026-06-11): packets that look like real
    new-info can be environmental artifacts invisible to the researcher —
    a leftover Terminal holding macOS foreground ate a complete 60-step
    keyboard BT and a DR nearly burned on it. The foreground_guard line in
    the tail carries the signal; when the LAST guard line shows
    frontmost != target, the failure is environmental: research must not
    fire, and the retry needs the environment cleared, not new knowledge."""
    import re as _re
    if not bt_debug_tail:
        return None
    guards = _re.findall(
        r"foreground_guard: frontmost='([^']+)' target='([^']+)'",
        str(bt_debug_tail),
    )
    if guards:
        frontmost, target = guards[-1]
        if frontmost != target:
            return (f"environmental/foreground — frontmost={frontmost!r} != "
                    f"target={target!r}; events/screenshots likely misrouted")
    return None


def classify_infra_failure(reason) -> Optional[str]:
    """Server/worker INFRA failures are never researchable: a worker_fallback
    means the pipeline crashed before producing a BT — there is nothing about
    the SCREEN for Perplexity/the Family to diagnose. Live 2026-06-11: argv
    limits, missing binaries and schema typos climbed the ladder to a Tier-3
    Family fan-out before being held by hand."""
    r = str(reason or "")
    if r.startswith("worker_fallback:"):
        return f"infra/worker_pipeline — {r[:160]}"
    return None


def dispatch_body_for_tier(
    *,
    tier: str,
    packet_path: Path,
    platform: str,
    screen_hash: str,
    retry_count: int,
    bt_debug_tail=None,
    reason=None,
) -> Optional[str]:
    """Build the DIRECT taeys-hands dispatch message for tier2/tier3.

    Returns None for tiers that have no fleet dispatch (tier1, terminal).
    Content mirrors what the tier templates previously told claude-primary
    to relay verbatim — the server now sends it itself.
    """
    env_class = classify_failure_environment(bt_debug_tail) or classify_infra_failure(reason)
    if env_class:
        logger.warning(
            f"escalation: SKIPPING tier-{tier[-1]} fleet dispatch for "
            f"{platform}/{screen_hash[:12]} — {env_class}. Research cannot "
            f"fix an environment problem; retry after the environment clears."
        )
        return None

    screenshot_path = packet_path.parent / "screenshot.png"
    reviews_name = f"{platform}_{screen_hash[:12]}_tier{tier[-1]}"
    response_routing = (
        f"ROUTE EVERY RESPONSE BACK via:\n"
        f"  taey-notify taey-ed --type response_ready --from taeys-hands\n"
        f"with the verbatim response saved to:\n"
        f"  /home/user/taey-ed/consultations/REVIEWS/{reviews_name}.md\n"
    )
    if tier == "tier2":
        return (
            f"ROUTE TO PERPLEXITY (Deep Research, display :6) — Escalation Tier 2 [AUTO-DISPATCHED]\n\n"
            f"REMINDER: prepend FAMILY_KERNEL.md and IDENTITY_CLARITY.md to the prompt —\n"
            f"Clarity needs constitutional context to respond as Clarity.\n\n"
            f"PACKET (read in full): {packet_path}\n"
            f"SCREENSHOT (attach to the Perplexity prompt): {screenshot_path}\n\n"
            f"DR MODE: confirm 'pressed' state on the Deep Research toggle before submit.\n\n"
            f"SUGGESTED RESPONSE STRUCTURE (prose is fine):\n"
            f"## Diagnosis (what's going wrong, source-cited)\n"
            f"## Definition fixes (classification, YAML edits, operational_notes, quirks)\n"
            f"## Handler/schema gaps (only if existing definitions cannot express the fix)\n"
            f"## Confidence (LOW/MED/HIGH and why)\n"
            f"## Open Questions\n\n"
            f"{response_routing}"
        )
    if tier == "tier3":
        return (
            f"ROUTE TO FULL FAMILY (parallel fan-out) — Escalation Tier 3, ONE round [AUTO-DISPATCHED]\n\n"
            f"Fan out to all 5 Family platforms in parallel: Gaia (Claude, :3),\n"
            f"Horizon (ChatGPT, :2), Cosmos (Gemini, :4), Logos (Grok, :5),\n"
            f"Clarity (Perplexity DR, :6).\n\n"
            f"REMINDER: prepend FAMILY_KERNEL.md and the per-platform\n"
            f"IDENTITY_<codename>.md to EACH platform's prompt.\n\n"
            f"PACKET (one document, send to all 5): {packet_path}\n"
            f"SCREENSHOT (attach where the platform accepts files): {screenshot_path}\n\n"
            f"{response_routing}"
        )
    return None


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

    # Family round annotation for Tier 3 (one round per Jesse 2026-06-11).
    family_loop_note = ""
    if tier == "tier3":
        family_loop_note = "\n- tier3_round: 1 of 1"

    if specific_ask is None:
        specific_ask = (
            f"Improve the DEFINITION path for this {platform} screen "
            f"(variant: {screen_type_hint}). Prior attempts have failed in the "
            f"manner shown in section 5. Recommend classification fixes, YAML edits, "
            f"operational_notes, platform quirks, or minimal handler/schema gaps if "
            f"the current definition surface cannot express the correct behavior. "
            f"Do NOT propose or return a behavior tree."
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

## Definition fixes
(classification, YAML edits, operational_notes, quirks, and exact provenance)

## Handler/schema gaps
(only if no definition-only fix is sufficient)

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

def _inline_screen_summary(diag_state_dir: Path) -> str:
    """Build an inline screen-state snapshot for the notification body.

    Reads tree.json (if present) for WebArea + role counts + sample buttons.
    Reads attempts.jsonl for last failure mode + BT shape + Mac log tail.
    Per Jesse 2026-05-19: notifications must carry actionable detail inline
    so claude doesn't have to open the packet to start working.
    """
    import json as _json
    lines = []

    # Tree.json — current screen state
    tree_path = diag_state_dir / "tree.json"
    if tree_path.exists():
        try:
            tree = _json.loads(tree_path.read_text())
            # WebArea name
            stack = [tree]; webarea = "?"
            while stack:
                n = stack.pop()
                if isinstance(n, dict) and n.get("role") == "AXWebArea":
                    webarea = (n.get("name") or "").strip() or "?"
                    break
                if isinstance(n, dict):
                    for c in n.get("children") or []:
                        stack.append(c)
            # FULL content dump (Jesse 2026-06-02): an escalated screen is UNKNOWN
            # by definition — the disambiguating signal (completion markers, real
            # question text, answer widgets) is NOT knowable in advance, it lives
            # ONLY in the raw tree. So dump every signal-bearing element here, in
            # the message itself, so the recipient cannot decide off a partial
            # button-sample. Scope to the page AXWebArea subtree(s) so browser
            # chrome (toolbar, bookmarks, the ~30 tab-strip AXRadioButtons) is
            # excluded; chrome lives outside AXWebArea.
            import re as _re
            webareas = []
            _st = [tree]
            while _st:
                n = _st.pop()
                if isinstance(n, dict):
                    if n.get("role") == "AXWebArea":
                        webareas.append(n)
                    for c in n.get("children") or []:
                        _st.append(c)
            SIGNAL = {"AXButton", "AXLink", "AXComboBox", "AXCheckBox",
                      "AXRadioButton", "AXPopUpButton", "AXTextField",
                      "AXTextArea", "AXImage", "AXProgressIndicator", "AXSlider"}
            elems = []           # (role, name, bbox)
            qtext = []           # content static text (the question)
            seen = set()

            def _collect(n):
                if not isinstance(n, dict):
                    return
                r = n.get("role", "")
                nm = (n.get("name") or n.get("title") or "").strip()
                v = n.get("value")
                if not nm and isinstance(v, str) and v.strip():
                    nm = v.strip()
                bb = n.get("visible_bbox") or n.get("bbox") or [0, 0, 0, 0]
                if r in SIGNAL and nm:
                    k = (r, nm[:90])
                    if k not in seen:
                        seen.add(k)
                        elems.append((r, nm, bb))
                elif r == "AXStaticText" and nm and len(nm) >= 25 and nm not in qtext:
                    qtext.append(nm)
                for c in n.get("children") or []:
                    _collect(c)

            roots = webareas if webareas else [tree]
            for rt in roots:
                for c in rt.get("children") or []:
                    _collect(c)

            def _y(b):
                return b[1] if isinstance(b, list) and len(b) > 1 else 0

            comp = [(r, nm, b) for (r, nm, b) in elems
                    if r == "AXProgressIndicator"
                    or _re.search(r"completed|\bcomplete\b|mastered|proficient|"
                                  r"up next|crown|great work|correct|not quite|"
                                  r"try again", nm, _re.I)]
            widgets = [(r, nm, b) for (r, nm, b) in elems if r in
                       ("AXComboBox", "AXPopUpButton", "AXCheckBox",
                        "AXRadioButton", "AXTextField", "AXTextArea")]
            buttons = [(r, nm, b) for (r, nm, b) in elems if r == "AXButton"]
            links = [(r, nm, b) for (r, nm, b) in elems if r == "AXLink"]
            images = [(r, nm, b) for (r, nm, b) in elems if r == "AXImage"]

            lines.append("SCREEN STATE (full content dump from tree — the answer is "
                         "in here; do NOT decide from a partial sample):")
            lines.append(f"  WebArea: {webarea!r}")
            if comp:
                lines.append("  COMPLETION / RESULT MARKERS (read FIRST — a "
                             "'completed <X>' / 'Mastered' / 'Great work' / 'Correct' "
                             "here means that item is DONE → advance, never redo; "
                             "'Try again'/'Not quite' = wrong-answer state):")
                for r, nm, b in comp[:30]:
                    lines.append(f"    - {r} y={_y(b):4d} {nm[:100]!r}")
            if qtext:
                lines.append("  QUESTION / CONTENT TEXT:")
                for t in qtext[:14]:
                    lines.append(f"    - {t[:180]!r}")
            if widgets:
                lines.append("  ANSWER WIDGETS:")
                for r, nm, b in sorted(widgets, key=lambda e: _y(e[2]))[:50]:
                    lines.append(f"    - {r} y={_y(b):4d} {nm[:80]!r}")
            if buttons:
                lines.append("  BUTTONS (content):")
                for r, nm, b in sorted(buttons, key=lambda e: _y(e[2]))[:50]:
                    lines.append(f"    - y={_y(b):4d} {nm[:80]!r}")
            if links:
                lines.append("  LINKS (content):")
                for r, nm, b in sorted(links, key=lambda e: _y(e[2]))[:50]:
                    lines.append(f"    - y={_y(b):4d} {nm[:80]!r}")
            if images:
                lines.append(f"  VISUAL CONTENT: {len(images)} content AXImage(s) "
                             "present — for graphs/diagrams/animations the screenshot "
                             "matters; Read screenshot.png before solving image-grounded items.")
        except Exception as e:
            lines.append(f"SCREEN STATE: (tree read failed: {e})")

    # attempts.jsonl — last failure
    attempts_path = diag_state_dir / "attempts.jsonl"
    if attempts_path.exists():
        try:
            last_attempt = None
            for raw_line in attempts_path.read_text().splitlines():
                raw_line = raw_line.strip()
                if not raw_line:
                    continue
                try:
                    last_attempt = _json.loads(raw_line)
                except Exception:
                    pass
            if last_attempt:
                lines.append("")
                lines.append("LAST ATTEMPT:")
                lines.append(f"  classification: {last_attempt.get('classification')!r}")
                lines.append(f"  failure_mode: {last_attempt.get('failure_mode')!r}")
                bt = last_attempt.get("bt") or {}
                if bt:
                    # Count actions instead of full BT (notification stays readable)
                    actions = []
                    def _count(n):
                        if isinstance(n, dict):
                            if n.get("type") == "action" and n.get("action"):
                                actions.append(n["action"])
                            for v in n.values():
                                _count(v)
                        elif isinstance(n, list):
                            for item in n:
                                _count(item)
                    _count(bt)
                    if actions:
                        lines.append(f"  BT actions ({len(actions)}): {', '.join(actions[:15])}{' ...' if len(actions) > 15 else ''}")
                mac_log = last_attempt.get("mac_log_tail") or ""
                if mac_log.strip():
                    tail = mac_log.strip().splitlines()[-12:]
                    lines.append("  Mac BT log tail:")
                    for ln in tail:
                        lines.append(f"    {ln[:140]}")
                else:
                    lines.append("  Mac BT log: (NOT PROVIDED by Mac — wire-format issue)")
        except Exception as e:
            lines.append(f"LAST ATTEMPT: (attempts.jsonl read failed: {e})")

    return "\n".join(lines)


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
    (what to do with it) AND now carries inline screen-state + last-attempt
    summary so claude-primary doesn't have to open files just to triage.
    """
    template = template_path_for_tier(tier)
    inline = _inline_screen_summary(diag_state_dir)
    base = (
        f"ESCALATION TIER {tier.upper()} — production loop\n"
        f"Platform: {platform}\n"
        f"Screen hash: {screen_hash}\n"
        f"Attempts so far: {retry_count}\n"
        f"State dir: {diag_state_dir}\n"
        f"SCREENSHOT (READ THIS FIRST — ground truth; the tree dump below can lag the live screen):\n"
        f"  {diag_state_dir}/screenshot.png\n"
        f"Consult: {consult_path}\n"
        f"\n"
        f"{inline}\n"
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
            "AUTO-DISPATCHED to taeys-hands (Perplexity DR) by the server — do NOT re-dispatch.\n"
            "ACTION: await the response_ready from taeys-hands, then SYNTHESIZE the research\n"
            "and fold it into knowledge.json as a PROVISIONAL operational_note for this screen.\n"
            "Touch diagnosis_done.flag only AFTER the fold — DO NOT touch gave_up.flag.\n"
        )
    if tier == "tier3":
        return base + (
            f"\n"
            f"AUTO-DISPATCHED to taeys-hands (full Family fan-out, one round) by the server — do NOT re-dispatch.\n"
            f"ACTION: await the response_ready messages, SYNTHESIZE the Family perspectives\n"
            f"(synthesis, not a vote) and fold the approach into knowledge.json as PROVISIONAL.\n"
            f"Touch diagnosis_done.flag only AFTER the fold — DO NOT touch gave_up.flag.\n"
            f"If this round fails: next escalation auto-triggers terminal.\n"
        )
    # terminal
    return base + (
        "\n"
        "ACTION: Mark unsolvable. Append entry to /home/user/taey-ed/consultations/UNSOLVED.md.\n"
        "Touch gave_up.flag. Send a summary defect notification.\n"
        "Follow terminal_giveup.md exactly.\n"
    )
