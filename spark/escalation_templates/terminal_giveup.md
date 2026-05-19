# Terminal Escalation — Mark Unsolvable

You hit Terminal because the full ladder has been exhausted (per Jesse
2026-05-19 FINAL ladder):
- Tier 1: 2 attempts by claude-primary editing knowledge.json
- Tier 2: 1 Perplexity DR consultation
- Tier 3: 2 full Family loops

That's 5 attempts total. This screen is genuinely outside the system's current
capability — likely a handler gap or a fundamentally novel widget pattern that
can't be solved with the existing primitives.

## What to do

1. Open the escalation packet for one last review. Confirm: was the failure mode
   consistent across tiers (suggests handler gap), or did each tier propose
   different things and none worked (suggests something subtler)?

2. Touch the gave_up.flag for this state dir. This is the ONLY tier where
   gave_up.flag is appropriate.

3. Log the unsolved screen to `/home/user/taey-ed/consultations/UNSOLVED.md`
   (create if missing). Append a section with:
   - escalation_id, platform, screen_hash, variant
   - Failure-mode summary (one paragraph)
   - The strongest candidate BT proposed across all tiers
   - The recommended handler addition / system change (Mac-side or server-side)
   - Pointer to the packet for forensic reference

4. Send a single notification summarizing the terminal state. Use the canonical
   notification system:

   ```
   /usr/local/bin/taey-notify taey-ed --type defect --from spark \
     "TERMINAL ESCALATION — <platform> screen_hash <hash> marked unsolvable
    after 6-tier exhaustion. Packet: <path>. Recommended next step:
    <one-line — usually a new Mac handler or server-side change>."
   ```

5. The Mac signature for this screen is automatically deleted by the system on
   gave_up.flag. The screen will not retry the same path. Mac surfaces it to
   the user as a known-unsolvable.

## What this means architecturally

A Terminal escalation is a **bug report on the system**, not a failure of the
escalation process. The escalation process did its job — it surfaced the gap.
The fix is upstream:
- A new Mac handler (`rank_items`, `text_drag`, `dialog_solve`, etc.)
- A new BT primitive (arithmetic? JSON-list parse?)
- A new question_type in send_to_llm
- An extension to the knowledge.json schema

UNSOLVED.md is the work-queue for those upstream fixes. Treasurer reviews it
periodically and dispatches the right fleet member (CCM for Mac, claude-primary
for server, codex/gemini for measurement).

## Anti-patterns

- DO NOT give up on Tier 1, 2, or 3 manually. The system auto-promotes.
- DO NOT touch gave_up.flag outside Terminal.
- DO NOT silently delete the state dir. UNSOLVED.md is the audit trail.
