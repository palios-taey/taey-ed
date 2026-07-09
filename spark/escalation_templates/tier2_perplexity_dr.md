# Tier 2 — Perplexity Deep Research (dispatch is automatic)

You hit Tier 2 because the same screen failed both your Tier-1 shots. The server
has **already dispatched** the packet to taeys-hands (Perplexity Deep Research,
display :6). Do **NOT** re-dispatch. Your job here is synthesis.

1. Open the escalation packet at the path in the notification — it carries
   identity, screen artifacts (tree.json + screenshot.png), system capabilities,
   and **ALL prior attempts**.
2. Wait for the `response_ready` notification (the verbatim response is saved
   under the configured data root: `TAEY_ED_DATA_DIR/consultations/REVIEWS/`).
   **Do NOT touch any flag
   while waiting** — auto-resume handles timing; there is no flag for you to set.
3. Synthesize the research and fold it into the **per-screen YAML**
   (`spark/platforms/<platform>/screen_types/<SCREEN_TYPE>.yaml`) — this is what
   the worker actually receives (NOT knowledge.json). Fold it as a PROVISIONAL
   refinement, keep every section internally consistent, and REMOVE/REPLACE the
   parts that produced the failing attempts (a failed answer is discarded, not
   kept alongside the new one).
4. **Do NOT touch any flag.** The system auto-resumes and the worker rebuilds
   from your YAML. If it fails again, the ladder auto-climbs to the Family.

Research is PROVISIONAL until a real run on the real screen proves it (the
3-validated-success promotion is automatic). Never hand the app a move yourself —
the worker builds from your YAML.
