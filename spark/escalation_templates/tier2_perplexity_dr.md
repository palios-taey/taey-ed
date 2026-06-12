# Tier 2 Escalation — Perplexity Deep Research Synthesis (dispatch is automatic)

You hit Tier 2 because the same screen has failed twice under Tier 1 (your
knowledge.json operational_note fix loop). You do NOT keep editing knowledge.json
blind at this tier — the rule must be informed by outside research first.

**The server has ALREADY dispatched the packet to taeys-hands** (Perplexity
Deep Research, display :6). Do NOT re-dispatch. Your job at this tier is the
synthesis.

## What to do

1. Open the escalation packet at the path the notification gave you. It contains:
   identity, screen artifacts (tree.json path + screenshot.png alongside),
   system capabilities, every prior attempt, and the specific ask.

2. Wait for Clarity's response (a `response_ready` notification arrives in
   your inbox; the verbatim response is saved under
   `/home/user/taey-ed/consultations/REVIEWS/`).

   DO NOT touch `diagnosis_done.flag` while waiting. The Mac stays in wait
   state until you touch it; touching it early makes the retry run WITHOUT
   the research.

3. Incorporate the findings into a PROVISIONAL DEFINITION update in
   `knowledge.json` for this screen. That can include classification fixes,
   YAML-aligned operational_notes, and platform quirks. Write any new note
   with `verified_count: 0` — research is unproven until a real run succeeds
   with it (the 0→1→2→3 credit path is automatic on validated successes).
   Only notes you have personally OBSERVED working get the verified_count=1
   default. While folding, also REPLACE or REMOVE the prior note(s) that
   produced the failing attempts — a failed provisional answer is discarded,
   not kept alongside the new one. Re-register the screen's hash if a failure
   path deleted it.

4. Never hand the Mac a proposed answer or BT from this tier. The supervisor
   role here is to improve definitions, not to execute as the worker.

5. ONLY AFTER the fold: touch `diagnosis_done.flag`. The server releases the
   Mac with the fresh knowledge.json in hand. The note stays provisional until
   a real validated run proves it (then it graduates per the 3-success rule).

6. If the screen still fails after this Tier 2 cycle, the system will trigger
   Tier 3 (full Family, one round) on the next escalation attempt — also
   auto-dispatched. Don't pre-escalate.

## Anti-patterns

- DO NOT re-dispatch to taeys-hands — the server already did.
- DO NOT touch diagnosis_done.flag before the knowledge.json fold is written.
- DO NOT touch gave_up.flag at this tier. Tier 3 is automatic.
