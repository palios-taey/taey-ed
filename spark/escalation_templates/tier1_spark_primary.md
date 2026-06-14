# Tier 1 — Operator (you): up to 2 shots

Your role and the full flow are authoritative in your CLAUDE.md and
`/home/user/taey-ed/INTENDED_FLOW.md` (§D ladder, §F roles). Read them.

1. **Read the SCREENSHOT first** — ground truth; logs lag and mislead.
2. **Review ALL prior attempts** in the packet (worker Tier-0 + your earlier
   shot): what each tried and exactly how it failed. **Never repeat a failed
   approach.** Diagnose **which step of extract → answer → enter** (or which
   missing input) broke — never invent a new mechanism.
3. **Fix the per-screen PROGRAM** — the YAML the worker actually receives:
   `spark/platforms/<platform>/screen_types/<SCREEN_TYPE>.yaml`.
   NOT `knowledge.json` (its notes do not reach the worker). Keep **every**
   section (recipe / template / contracts / actuation / verification)
   internally consistent — contradictions make the worker freelance.
4. **Do NOT touch any flag.** The system auto-resumes on its own and the worker
   rebuilds from your YAML on the next cycle. Editing the YAML IS the whole job.

You never hand a move to the app directly — the worker builds the action from
your YAML, which validates the production worker can actually do it. If the worker
can't, the **YAML** is wrong.

If you can't fix it within the design in 2 shots, the ladder **auto-climbs** to
Perplexity. You do nothing to advance it.
