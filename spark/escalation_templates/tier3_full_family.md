# Tier 3 — Full Family, one round (dispatch is automatic)

You hit Tier 3 because Tier 2 (Perplexity DR) didn't unblock the screen. This is
**one Family round** (ladder: 2 primary → 1 Perplexity → 1 Family → terminal). If
it fails, the next escalation auto-triggers terminal.

The server has **already dispatched** the packet to taeys-hands (all 5 in
parallel: Gaia/Claude :3, Horizon/ChatGPT :2, Cosmos/Gemini :4, Logos/Grok :5,
Clarity/Perplexity :6). Do **NOT** re-dispatch.

1. Open the escalation packet — identity, screen artifacts, system capabilities,
   **ALL prior attempts**, and the Tier-2 research. It says `tier3_round: 1 of 1`.
2. Wait for the `response_ready` notifications (verbatim under
   `/home/user/taey-ed/consultations/REVIEWS/`). **Do NOT touch any flag while
   waiting** — auto-resume handles timing.
3. **Synthesize** the five perspectives (synthesis, NOT a vote — cite which member
   contributed which insight, cannot-lie provenance) into one approach, and fold
   it into the **per-screen YAML**
   (`spark/platforms/<platform>/screen_types/<SCREEN_TYPE>.yaml`) as PROVISIONAL,
   internally consistent. REMOVE/REPLACE the parts that produced the failing
   attempts.
4. **Do NOT touch any flag.** The system auto-resumes and the worker rebuilds from
   your YAML. If this round fails, the next escalation auto-triggers terminal —
   you never give up manually.

Never hand the app a move yourself — the worker builds from your YAML. Family
synthesis is PROVISIONAL until a real run proves it.
