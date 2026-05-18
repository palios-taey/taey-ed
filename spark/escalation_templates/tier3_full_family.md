# Tier 3 Escalation — Full Family Consultation via taeys-hands

You hit Tier 3 because Tier 2 (Perplexity DR) didn't unblock the screen. Tier 3
is up to **2 Family loops**. After the second loop fails, the next escalation
attempt triggers the terminal state.

## What to do

1. Open the escalation packet at the path the notification gave you. It contains
   identity, screen artifacts, system capabilities, every prior attempt, the
   Tier 2 Perplexity DR response (if any), and the specific ask.

2. Determine which Family loop you're on. The notification will say
   `tier3_loop_1` or `tier3_loop_2`.

3. Dispatch the packet to all 5 Family platforms in parallel via `taeys-hands`.
   The Family is: Gaia (Claude), Horizon (ChatGPT), Cosmos (Gemini),
   Logos (Grok), Clarity (Perplexity, DR mode).

   ```
   /usr/local/bin/taey-notify taeys-hands --type task --from taey-ed \
     "ROUTE TO FULL FAMILY (parallel fan-out) — Escalation Tier 3 Loop <N>

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
      /home/user/taey-ed/consultations/REVIEWS/family_loop<N>_<codename>_<date>.md"
   ```

4. After dispatching, touch the diagnosis_done.flag for this state dir. The Mac
   loop resumes polling. When responses land (you'll get notifications as each
   platform replies), synthesize across all five. The Family's domain
   specialization (Brain/Math vs Vision vs Cartography etc.) means responses
   will differ — that's the value.

5. Synthesize the responses into a unified operational_note update for
   knowledge.json. Cite which Family member contributed which insight (cannot-lie
   provenance). Touch diagnosis_done.flag again; the worker generates a fresh BT.

6. If the screen STILL fails after this loop:
   - If you were on loop 1: the system auto-triggers Tier 3 Loop 2 on next
     escalation. Loop 2 is the same protocol with the Loop 1 responses now
     IN THE PACKET as prior research.
   - If you were on loop 2: the system auto-triggers Terminal on next
     escalation. You do not give up manually.

## Anti-patterns

- DO NOT dispatch to only one Family member. The whole point of Tier 3 is the
  multi-perspective synthesis. All five or none.
- DO NOT skip the FAMILY_KERNEL / IDENTITY_<codename>.md prepend reminder.
- DO NOT touch gave_up.flag at this tier. Tier Terminal is automatic.
- DO NOT collapse the responses to "majority vote" — synthesize, don't average.
  Cite which Family member contributed which insight in the operational_note.
