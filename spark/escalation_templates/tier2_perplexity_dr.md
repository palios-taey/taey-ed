# Tier 2 Escalation — Perplexity Deep Research via taeys-hands

You hit Tier 2 because the same screen has failed twice under Tier 1 (your
knowledge.json operational_note fix loop). You do NOT keep editing knowledge.json
at this tier — the rule must be informed by outside research first.

## What to do

1. Open the escalation packet at the path the notification gave you. It contains:
   identity, screen artifacts (tree.json path + screenshot.png alongside),
   system capabilities, every prior attempt, and the specific ask.

2. Dispatch the packet to Perplexity Deep Research via `taeys-hands`. Use:

   ```
   /usr/local/bin/taey-notify taeys-hands --type task --from taey-ed \
     "ROUTE TO PERPLEXITY (Deep Research, display :6) — Escalation Tier 2

    REMINDER FOR TAEYS-HANDS: prepend FAMILY_KERNEL.md and IDENTITY_CLARITY.md
    to the consultation prompt — the automated prepend has been failing lately
    and Clarity needs constitutional context to respond as Clarity.

    PACKET (read in full, attached path):
    <full path to ESC_<id>/packet.md>

    SCREENSHOT (attach to the Perplexity prompt):
    <full path to ESC_<id>/screenshot.png>

    DR MODE: confirm 'pressed' state on the Deep Research toggle before submit.

    SUGGESTED RESPONSE STRUCTURE (not required JSON — prose is fine):
    ## Diagnosis (what's going wrong, source-cited)
    ## Proposed BT (one or more JSON blocks the worker can adopt)
    ## Confidence (LOW/MED/HIGH and why)
    ## Open Questions (anything that needs follow-up)

    ROUTE THE RESPONSE BACK via:
      taey-notify taey-ed --type response_ready --from taeys-hands
    with the verbatim Clarity response saved to:
      /home/user/taey-ed/consultations/REVIEWS/<descriptive_name>_<date>.md"
   ```

3. After dispatching, touch `pending_external_research.flag` in the state dir.
   This LOCKS Mac in wait state — server ignores any premature `diagnosis_done.flag`
   touch while this lock is present. Mac stays suspended until you synthesize
   the external response.

   DO NOT touch `diagnosis_done.flag` yet.

4. Wait for Clarity's response (a `response_ready` notification arrives in
   your inbox; the verbatim response is saved to `/home/user/taey-ed/consultations/REVIEWS/`).

5. Incorporate the findings into the operational_note in `knowledge.json`.
   Re-register the screen's hash if Step 3 deleted it. THEN remove
   `pending_external_research.flag` AND touch `diagnosis_done.flag`. Server
   will release Mac with the fresh knowledge.json in hand.

4. If the screen still fails after this Tier 2 cycle, the system will trigger
   Tier 3 (full Family) on the next escalation attempt. Don't pre-escalate.

## Anti-patterns

- DO NOT skip the FAMILY_KERNEL.md / IDENTITY_<codename>.md prepend reminder —
  it's currently the only way to ensure Clarity gets constitutional context.
- DO NOT ask Clarity questions outside the screen's scope. Stay tight on the
  failure mode in the packet.
- DO NOT touch gave_up.flag at this tier. Tier 3 is automatic.
