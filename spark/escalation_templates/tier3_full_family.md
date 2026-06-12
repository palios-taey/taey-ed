# Tier 3 Escalation — Full Family Synthesis (dispatch is automatic)

You hit Tier 3 because Tier 2 (Perplexity DR) didn't unblock the screen. Tier 3
is **one Full Family round** (Jesse 2026-06-11 canonical ladder:
2 primary → 1 Perplexity → 1 Family → terminal). If this round fails, the next
escalation attempt triggers terminal.

**The server has ALREADY dispatched the packet to taeys-hands** (all 5 Family
platforms in parallel: Gaia/Claude :3, Horizon/ChatGPT :2, Cosmos/Gemini :4,
Logos/Grok :5, Clarity/Perplexity-DR :6). Do NOT re-dispatch. Your job at this
tier is the synthesis.

## What to do

1. Open the escalation packet at the path the notification gave you. It contains
   identity, screen artifacts, system capabilities, every prior attempt, the
   Tier 2 Perplexity DR response (if any), and the specific ask. The notification
   says `tier3_round: 1 of 1`.

2. Wait for responses. Each platform replies via taeys-hands → `response_ready`
   notification in your inbox; verbatim responses land under
   `/home/user/taey-ed/consultations/REVIEWS/`.
   The Family's domain specialization (Brain/Math vs Vision vs Cartography etc.)
   means responses will differ — that's the value.

   DO NOT touch `diagnosis_done.flag` while waiting. The Mac stays in wait
   state until you touch it; touching it early makes the retry run WITHOUT
   the research.

3. Synthesize the responses into a unified PROVISIONAL operational_note update
   for `knowledge.json`. Write it with `verified_count: 0` — Family synthesis
   is unproven until a real run succeeds with it (the credit path promotes it
   automatically). Cite which Family member contributed which insight
   (cannot-lie provenance). REPLACE or REMOVE the prior note(s) that produced
   the failing attempts — failed provisional answers are discarded. Re-register
   the screen's hash if a failure path deleted it.

4. ONLY AFTER the fold: touch `diagnosis_done.flag`. The worker generates a
   fresh BT with the synthesized note. The note stays provisional until a real
   validated run proves it (then it graduates per the 3-success rule).

5. If the screen STILL fails after this round, the system auto-triggers
   Terminal on the next escalation. You do not give up manually.

## Anti-patterns

- DO NOT re-dispatch to taeys-hands — the server already did. One round only.
- DO NOT collapse the responses to "majority vote" — synthesize, don't average.
  Cite which Family member contributed which insight in the operational_note.
- DO NOT touch diagnosis_done.flag before the knowledge.json fold is written.
- DO NOT touch gave_up.flag at this tier. Terminal is automatic.
