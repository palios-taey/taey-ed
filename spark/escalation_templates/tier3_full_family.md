# Tier 3 Escalation — Full Family Consultation via taeys-hands

You hit Tier 3 because Tier 2 (Perplexity DR) didn't unblock the screen. Tier 3
is **one Full Family round** (Jesse 2026-06-11 canonical ladder:
2 primary → 1 Perplexity → 1 Family → terminal). If this round fails, the next
escalation attempt triggers terminal.

## What to do

1. Open the escalation packet at the path the notification gave you. It contains
   identity, screen artifacts, system capabilities, every prior attempt, the
   Tier 2 Perplexity DR response (if any), and the specific ask.

2. Confirm you're on the single Family round. The notification will say
   `tier3_round: 1 of 1`.

3. Dispatch the packet to all 5 Family platforms in parallel via `taeys-hands`.
   The Family is: Gaia (Claude), Horizon (ChatGPT), Cosmos (Gemini),
   Logos (Grok), Clarity (Perplexity, DR mode).

   ```
   /usr/local/bin/taey-notify taeys-hands --type task --from taey-ed \
     "ROUTE TO FULL FAMILY (parallel fan-out) — Escalation Tier 3

    REMINDER FOR TAEYS-HANDS: prepend FAMILY_KERNEL.md and the appropriate
    per-platform IDENTITY_<codename>.md to each platform's prompt — the
    automated prepend has been failing lately and each Family member needs
    their own constitutional context.

    PACKET (one document, send to all 5):
    <full path to ESC_<id>/packet.md>

    SCREENSHOT (attach to each platform's prompt):
    <full path to ESC_<id>/screenshot.png>

    FAN-OUT TARGETS (run in parallel, each on its own display):
    - Gaia (Claude, :3, Adaptive Thinking ON)
    - Horizon (ChatGPT, :2, Extended Thinking ON)
    - Cosmos (Gemini, :4, Deep Think ON)
    - Logos (Grok, :5, Heavy mode)
    - Clarity (Perplexity, :6, Deep Research toggle CONFIRMED PRESSED)

    SUGGESTED RESPONSE STRUCTURE (prose is fine, no JSON requirement):
    ## Diagnosis (your domain take on what's going wrong)
    ## Proposed BT (one or more JSON blocks, or 'no proposal')
    ## Confidence (LOW/MED/HIGH and why)
    ## Open Questions (anything for the next loop or follow-up)

    ROUTE EACH RESPONSE BACK via:
      taey-notify taey-ed --type response_ready --from taeys-hands
    with the source platform clearly labeled and the verbatim response saved to:
      /home/user/taey-ed/consultations/REVIEWS/family_<codename>_<date>.md"
   ```

4. After dispatching, touch `pending_external_research.flag` in the state dir.
   This LOCKS Mac in wait state — server ignores any premature `diagnosis_done.flag`
   touch while this lock is present. Mac stays suspended until you synthesize
   the responses.

   DO NOT touch `diagnosis_done.flag` yet.

5. Wait for responses. Each platform replies via taeys-hands → `response_ready`
   notification in your inbox; verbatim responses at
   `/home/user/taey-ed/consultations/REVIEWS/family_<codename>_*.md`.
   The Family's domain specialization (Brain/Math vs Vision vs Cartography etc.)
   means responses will differ — that's the value.

6. Synthesize the responses into a unified operational_note update for
   `knowledge.json`. Cite which Family member contributed which insight
   (cannot-lie provenance). Re-register the screen's hash if Step 3 deleted it.
   THEN remove `pending_external_research.flag` AND touch `diagnosis_done.flag`.
   The worker generates a fresh BT with the synthesized note.

6. If the screen STILL fails after this round, the system auto-triggers
   Terminal on the next escalation. You do not give up manually.

## Anti-patterns

- DO NOT dispatch to only one Family member. The whole point of Tier 3 is the
  multi-perspective synthesis. All five or none.
- DO NOT skip the FAMILY_KERNEL / IDENTITY_<codename>.md prepend reminder.
- DO NOT touch gave_up.flag at this tier. Tier Terminal is automatic.
- DO NOT collapse the responses to "majority vote" — synthesize, don't average.
  Cite which Family member contributed which insight in the operational_note.
