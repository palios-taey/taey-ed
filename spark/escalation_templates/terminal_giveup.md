# Terminal — system bug / capability gap (handed UP to the Supervisor)

The full ladder is exhausted (2 Operator shots → 1 Perplexity DR → 1 Family
round = 4 attempts). This screen is outside the system's current capability —
usually a missing handler or a novel widget pattern the existing primitives can't
express.

Terminal is **code-owned** (`escalation_state`): the system marks it terminal,
deletes the Mac signature so it won't retry the same path, and routes a **defect
notification to the Supervisor automatically**. You do not drive any of that.

## What you do

1. Review the packet once. Was the failure mode consistent across tiers (→ a
   handler gap), or did each tier try different things and none worked (→ subtler)?
2. Append an entry to `/home/user/taey-ed/consultations/UNSOLVED.md` (create if
   missing): escalation id, platform, screen_hash, variant, a one-paragraph
   failure-mode summary, the strongest approach tried across tiers, and your best
   read on the **missing capability** (new handler / primitive / question_type /
   schema extension).
3. **Do NOT touch any flag.** Terminal is code-owned; there is no flag for you to
   set. You do NOT fix the system.

A terminal screen is a **bug report on the system**, not a failure of the
process — the process did its job by surfacing the gap. The **Supervisor** owns
the upstream fix. UNSOLVED.md is that work-queue.

## Anti-patterns
- DO NOT give up manually at Tier 1/2/3 — the ladder auto-climbs.
- DO NOT touch any flag — escalation state is code-owned.
- DO NOT delete the state dir — UNSOLVED.md is the audit trail.
