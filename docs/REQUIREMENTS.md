# Taey-Ed REQUIREMENTS — The Canon
**Status: DRAFT v0 (2026-07-09)** — curated by the supervisor from standing directives; pending (1) merge of the verbatim evidence sweep (`dispatches/2026-07-09_requirements_evidence_sweep.md`) and (2) Jesse's ratification. Once ratified, this document is **the contract**: every phase of `taey-ed-state-context` (and any future change) is validated against it. Conflicts between code and this canon are bugs in the code.

Sources are cited per rule: `[CLAUDE.md]` = project instructions; `[mem:<slug>]` = project memory; `[yaml:<screen_type>]` = screen-type recipe contract; `[J:<date>]` = direct Jesse directive.

---

## 0. Mission & scale
- **R0.1** The system autonomously completes courses on educational platforms: navigate, watch, read, solve, advance — with minimal user action. `[CLAUDE.md]`
- **R0.2 SCALE:** must support **100s–1000s of platforms, figured out dynamically**. Zero per-platform code anywhere in the flow; a new platform costs one knowledge-research cycle, no code or schema changes. `[J:2026-07-09]`
- **R0.3** All intelligence is server-side and platform-generic: the model classifies, the model builds behavior trees, recipes + knowledge are data, never code. No hardcoded screen types, no platform-specific logic in the flow. (CLAUDE.md's "Gemini decides" wording is stale — the worker LLM is Claude since the June cutover; the PRINCIPLE — model decides, not code — is unchanged.) `[CLAUDE.md Cardinal Rule 1; git-history mine 2026-07-09]`
- **R0.4** No thresholds in tree analysis: presence only, never quantity heuristics — classification is the model's job. `[CLAUDE.md Cardinal Rule 2]`

## 1. Architecture hard lines
- **R1.1 The Mac app is FROZEN — dumb primitives only** (capture tree + screenshot, execute BT actions, report results). It is never modified to work around a server-side gap; every fix works with the tree the Mac already sends, which contains every distinguishing feature of every screen. `[J:2026-07-03/05-12]`
- **R1.2** The user's KB (content + vectors) lives on the user's Mac (sqlite+faiss) and **never leaves it**. Server sees only: text→embed requests, and a question's top-K already-retrieved chunks on a single consultation. `[CLAUDE.md hard line, J:2026-05-12]`
- **R1.3** The Mac never touches server-side databases; the wire contract is HTTP (`/next_action`, `/api/v1/embed`) only. `[CLAUDE.md]`
- **R1.4** Server state must be durable and transactional: no coordination via /tmp flag files, no failure cached as success, no content-hash as identity, no silent state loss on restart. `[J:2026-07-09; taey-ed-state-context plan]`

## 2. Screen model
- **R2.1** Six universal screen categories (NAVIGATION, VIDEO, ARTICLE, EXERCISE, TRANSITION, UNKNOWN); variants/subtypes extend them (e.g. `EXERCISE_DROPDOWN`). `[CLAUDE.md]`
- **R2.2** Deterministic types (VIDEO, ARTICLE) may store permanent BTs; dynamic types (EXERCISE, NAVIGATION, TRANSITION) are rebuilt fresh — content changes between encounters. `[CLAUDE.md Cardinal Rules 8-9]`
- **R2.3** Deterministic live-widget reads outrank cache, hint, and LLM opinion when inferring an exercise subtype (the widget in the tree is ground truth). `[mem:skeleton_maxdepth_truncates_widgets]`
- **R2.4** Navigation screens stay **LLM-driven** — tighten prompts/handler shape, never replace with server-side rule resolvers. `[mem:llm_required_for_nav_screens, J:2026-05-19]`
- **R2.5** A failed or UNKNOWN classification is never terminal truth: it must remain re-queueable (bounded), and must never be served as a completed result. `[mem:classify_cache_unknown_trap]`
- **R2.6** Screen identity must be resolvable without silent fusion: if two different screens produce the same fingerprint, the system must represent the ambiguity and disambiguate — never silently treat them as one. `[mem:shared_skeleton_quiz_advance_and_label; taey-ed-state-context p3]`

## 3. Exercise discipline
- **R3.1 ONE TRY ONLY.** Stuck (screen unchanged after action) or wrong answer = full stop and ask; no silent retries, no re-pick, no resubmit. `[CLAUDE.md Cardinal Rule 3; yaml:*/failure_modes]`
- **R3.2 Never SKIP work.** "Skip" buttons are banned. "Up Next" is banned **as a skip** — but clicking "Up Next" is CORRECT when it is the navigation to the next item AND the current screen shows a completion indicator (exercise solved / video complete). Completion first, then advance by whatever control the platform offers. `[CLAUDE.md Cardinal Rule 4, corrected J:2026-07-09]`
- **R3.3 Question context is captured on every exercise**: the question (verbatim), the enumerated options, the chosen answer, and correctness — persisted (store_qa / qa_captures) so solved screens are never re-derived from scratch. `[J:2026-07-09; yaml:* store_qa steps]`
- **R3.4 Enumerate before ask:** when options live in a widget (dropdown menus), open + enumerate them and pass the exact option set to the LLM; the answer must be selected from the enumerated set, never free-generated. `[yaml:EXERCISE_DROPDOWN contracts]`
- **R3.5** Answer widgets are targeted by element reference + position, never by name/label when names are identical. `[yaml:EXERCISE_DROPDOWN contracts]`
- **R3.6** The extraction BT pulls the real question + full structure from the content pane — never page title/nav chrome. `[mem:capture_main_pane_not_chrome]`
- **R3.7** Wrong-answer detection: same exercise screen (same skeleton) after submit = wrong answer or nothing staged — never success. `[yaml:* verification]`

## 4. Image / vision escalation ladder
- **R4.1 Tier 0 — the tree, free:** figures often carry the exact answer in AXImage alt-text; read the tree FIRST. `[mem:alttext_tier0_supersedes_grid_cv]`
- **R4.2 Tier 1 — screenshot vision:** solve calls attach the screenshot; labels rendered as images/MathML (absent from AX text) are read from the screenshot by position. `[yaml:EXERCISE_DROPDOWN contracts]`
- **R4.3 Tier 2 — CV/grid measurement:** computer-vision measurement (grid CV etc.) only as the alt-text-absent fallback, never as the first move. `[mem:alttext_tier0_supersedes_grid_cv]`
- **R4.4 Screenshot-first diagnosis:** on every escalation, the screenshot is ground truth and is read before any log/tree/hash reasoning. `[mem:look_at_screenshot_first, J:2026-06-01]`

## 5. Video / article semantics
- **R5.1** Completion indicators gate the advance — never player presence, never "something moved". A playing state is not success. `[commit 78810a9; mem:video_complete_is_up_next_present]`
- **R5.2** "Up next" link present = the video is DONE → advance; never re-play a completed video. `[mem:video_complete_is_up_next_present, J:2026-06-02]`
- **R5.3** The completion check runs every poll cycle (a finished video's tree hash freezes; a check gated on tree-change never fires). `[commit 832a397]`
- **R5.4** `video_poll` must be the only child in its sequence. `[CLAUDE.md Cardinal Rule 5]`
- **R5.5** Completion indicators are platform DATA (recipe YAML / knowledge), not literals in route code. `[CLAUDE.md Cardinal Rule 1; taey-ed-state-context p5-declarative]`

## 6. Actuation rules
- **R6.1** `match_mode` is **exact only**; `contains` is banned. `[mem:no_match_mode_contains, J:2026-05-18]`
- **R6.2** Browsers get `mouse_click`; `ax_press` silently no-ops on Chrome and is banned for browser elements. `[CLAUDE.md Cardinal Rule 10]`
- **R6.3** Composable nodes (`for_each`, `conditional`) take parameters at TOP LEVEL, never inside `params:`. `[CLAUDE.md Cardinal Rule 6]`
- **R6.4** No `fallback` nodes in worker-rebuilt BTs (rejected); `fallback` is legitimate inside deterministic replayed fixed BTs. `[mem:fallback_safe_in_replay_banned_in_rebuild]`
- **R6.5** Selection widgets use their dedicated handler (`select_dropdown_option` owns the strategy ladder + verify); never hand-rolled clicks on menu items; never coordinate-clicks (`click_at`) to open triggers. `[yaml:EXERCISE_DROPDOWN contracts]`
- **R6.6** Presence gates over blind sleeps: `wait_for_element` on the concrete role gates actions on widgets that hydrate/flicker; a naked find after a blind wait is a defect. `[commit bf3ad1d]`
- **R6.7** Invariant safety lives in the ENGINE, not in recipe trust: registered-handler floor, navigate deny-lists — a worker/LLM output can never bypass an engine floor. `[mem:navigate_skip_and_safestop]`

## 7. Context & truncation
- **R7.1 NO TRUNCATION — EVER, ANYWHERE.** Vectors, text, tokens, payloads: never silently capped or cut. If a budget is hit, whole optional slices are dropped WITH an explicit logged receipt of what and why — or the call refuses and escalates. Change config or escalate; never silently trim. `[mem:no_truncation_ever, J:2026-05-12]`
- **R7.2** Classifier/worker input is filtered by ALLOWLIST (keep content: names/values; drop coords; collapse empty groups) — an unfiltered tree starves the model and produces silent UNKNOWNs. `[mem:tree_filter_starves_classifier; commit 557bfa8]`
- **R7.3** Context is scoped per call (JIT): only the relevant handlers, question types, knowledge slices, and per-screen history for THIS screen — never the full doc dump. `[CLAUDE.md anti-pattern "Send all docs"; J:2026-07-09]`
- **R7.4** Required context missing = refuse and escalate (fail-closed), never freelance under-contexted. `[taey-ed-state-context p4; J:2026-07-09]`
- **R7.5** Every LLM call's served context is receipted (what was included, what was dropped, why) for audit. `[taey-ed-state-context p4]`

## 8. THE ESCALATION PROCESS (complete, as-built — reconstructed 2026-07-09)
> Full state machine + lifecycle: `dispatches/discovery2/escalation_process.md` (9 states, ~24 transitions, all file:line-cited). The rules below are the binding distillation.

### The ladder
- **R8.1 Single funnel.** Every failure class (worker fallback, wrong answer, not-advanced, stuck, staging-stuck, UNKNOWN, missing artifact, user message) routes through the ONE canonical escalation helper; the Mac app and the API never escalate to the user directly. A user chat message is URGENT: stop everything, escalate immediately, never queue to the worker. `[next_action.py:76-101, 1180-1337, 1351-1376; J:2026-05-18, 2026-06-01]`
- **R8.2 The ladder (definitive):** worker 1 shot per consult → Tier 1 Operator 2 shots (per-screen YAML edit) → Tier 2 Perplexity Deep Research 1 shot → Tier 3 full Family ONE round → terminal. 4 attempts total. The user never performs an escalation. `[escalation.py:44-64; INTENDED_FLOW §D + Decisions #2; commits 47cd946/0f23c3e/4fb1ade; J:2026-06-11]`
- **R8.3 Monotonic, code-owned state.** Attempt count + terminal live in a code-owned store; no flag manipulation advances, resets, or un-terminates the ladder ("62 worker builds on ONE screen, ~$1.2k/day"). Terminal is sticky. `[escalation_state.py:1-18, 74-80; commit 0f23c3e; J:2026-06-14]`
- **R8.4 Climb on real attempts only.** The ladder advances once per genuinely DISTINCT failed attempt (deduped by consult id) — never on a timer, never on re-reads of the same failure. Environmental failures (foreground-window mismatch) and infra crashes are NOT attempts. `[escalation_state.py:52-71; commits 97bdaf7, 4fa37d9, dd81663; J:2026-06-14]`
- **R8.5 Exactly three resets:** user-Stop (abandon / session-reset), genuine screen advance, and a YAML fold (recipe edited after the last attempt ⇒ automatic clean ladder — stale failures never carry forward against a fixed program). A fold does NOT un-terminate a terminal screen; only user-Stop/advance do. `[escalation_state.py:83-107; next_action.py:172-192, 1573-1578; consultation.py:128-137; commits f1c718d, 0db433d; J:2026-06-14, 2026-05-20]`

### Consultation lifecycle
- **R8.6 ONE consultation at a time, globally.** A pending consult blocks all new consults; while any consult or diagnosis is open anywhere, every screen waits (global central-feedback lock, 30s poll). Stale pendings self-heal via TTL (600s). `[consultation_request.py:40-88; next_action.py:1191-1302; J:2026-05-18]`
- **R8.7 Worker gets ONE shot and never hangs the Mac.** Any generation failure writes an explicit fallback response, which escalates AND invalidates the consult so the next cycle builds fresh from the current YAML — a cached failure is never re-served. `[consultation_worker.py:93-127; next_action.py:1444-1491; J:2026-05-18]`
- **R8.8 UNKNOWN never reaches the worker.** UNKNOWN means "operator mapping required"; the worker only ever receives a classified screen. `[next_action.py:795-817; commit 557bfa8; J:2026-06-15]`
- **R8.9 Responses are definitions, never answers.** Consult responses are classifications/YAML definitions; the corrected-answer channel to the Mac is disabled; the operator never hands the app a move — the worker building from the YAML is the proof. `[consultation.py:52-56; action_review.py:159-160; tier1 template]`

### Escalation content & dispatch
- **R8.10 Packet completeness, screenshot-first.** Every escalation produces a single-folder packet (packet.md + CURRENT screenshot + CURRENT tree, always-overwritten — never stale) with full attempt history, capabilities snapshot, operational notes. The screenshot is read FIRST — ground truth. `[escalation.py:337-485; next_action.py:50-73, 119-140; J:2026-06-02/06-11]`
- **R8.11 Auto-climb dispatch, server-side.** Tier 2/3 packets are dispatched to taeys-hands BY THE SERVER (Perplexity DR / all-5 Family fan-out with FAMILY_KERNEL + IDENTITY prepends); the operator is a synthesizer, never a relay; responses return verbatim as `response_ready`. Every escalation emission site must actually dispatch. `[escalation.py:79-204; next_action.py:336-353; INTENDED_FLOW §D]`
- **R8.12 Dispatch once per {screen, tier}** — deduped by REVIEWS-file existence + per-tier marker (cleared with the diag dir on advance/user-Stop). `[next_action.py:354-374; operator defect 2026-06-14]`
- **R8.13 Research is never burned on non-screen failures.** Environmental + infra failures SKIP Tier-2/3 dispatch — retry needs the environment/pipeline fixed, not new knowledge. `[escalation.py:106-166; incidents 2026-06-11]`
- **R8.14 Tier-3 is synthesis, not a vote,** with per-member provenance; ONE round only, then terminal. `[escalation.py:392-395, 720-728]`
- **R8.15 Everything from research/Family/user is PROVISIONAL** until a real run on the real screen proves it; failed answers are removed, not kept alongside; 3 validated successes promote. `[INTENDED_FLOW §D-E; tier2/3 templates]`

### Resume
- **R8.16 Resume is code-automatic; humans and AIs touch NO flags.** Per-tier dead-man windows (tier1 180s; tier2/3 1200s) force a fresh attempt if the operator/research stalls; the window RE-TRIGGERS an attempt but NEVER advances the tier. Completion is response-driven in design: response_ready → operator fold → fold-reset gives the re-attempt a clean ladder. `[next_action.py:202-205, 267-283, 1237-1281; commit 01ff53e; J:2026-06-14]`
- **R8.17 Auto-resume abandons the stale consult** so the re-attempt mints a fresh build from the current YAML. `[next_action.py:1262-1277]`

### Terminal & user
- **R8.18 Terminal is the system giving up, never a person.** Ladder exhaustion auto-marks unsolvable, appends `consultations/UNSOLVED.md`, and routes a defect-typed hand-up to the SUPERVISOR (routine tiers → OPERATOR). A terminal screen is a bug report on the system — usually a missing capability; the Supervisor owns the upstream fix. `[next_action.py:224-263; notify_tmux.py:19-30; commit b8a16a4]`
- **R8.19 Terminal user-assist:** the user's dialog carries the operator-authored exact steps/answer verbatim — the user is helped, not just stopped. `[next_action.py:145-170; commit c5c8403; J:2026-06-11]`
- **R8.20 The only real `user_input_needed` states** are terminal screens and dialogs whose answer re-enters as user guidance (one consult); everything else claiming to need the user is intercepted and escalated. `[next_action.py:1180-1337, 1925-1952]`
- **R8.21 Wrong answer = verdict-first, immediate escalation, no reflex resubmit.** A wrong verdict overrides any tree movement; RCA to the determinable cause is required (mastery loss is unrecoverable — prevention is the point). `[next_action.py:884-901, 1623-1672; commit 2f4a245; J:2026-06-14]`

### Image ladder (interleave)
- **R8.22 Image answers climb a deterministic-first ladder inside serving:** platform alt-text (Tier 0, ground truth, free) → screenshot vision → deterministic CV. Engine-injected universally (not per-YAML), relevance-first; complex-enter types (sorter/matcher/graph/label) are exempt from the alt-text shortcut because the answer must still be ENTERED. CV is hard-gated "deterministic-when-proven, escalate-when-not": SHADOW measures + logs + ALWAYS escalates; SUBMIT requires an explicit flag AND CV self-confidence; any refusal feeds the tier ladder as a BT failure. `[next_action.py:671-720; call_gemini.py:165-255; measure_grid.py:1-33; commits 6711685/a846612/b97cd4b/ae88972/f1b1463; J:2026-06-15]`

### Cost & loop discipline
- **R8.23 Anti-runaway floors are ENGINE-owned:** monotonic counter, no timer-climb, ONE-consult gate, ONE TRY ONLY, loop guards (variant=1 fail, staging=3 cycles, hydration=6 polls), dispatch dedup, TTL self-heal, validated-map demote-at-2-never-delete-on-one-off. No recipe, worker output, or operator action may bypass them. `[escalation_state.py:1-18; next_action.py:396-434, 402-411, 1110-1124, 1987-2011]`
- **R8.24 Escalations never bill:** credits debit on successful screen completion only — polls, waits, stuck, retries, wrong-answer escalations do not. `[code.bak/LAUNCH_PLAN.md:91]`
- **R8.25 Knowledge gate:** a platform with no knowledge.json BLOCKS (Perplexity research requested once, flagged, waited) — never build blind on an unknown platform. `[next_action.py:2250-2295]`
- **R8.26 Fleet-ops visibility:** every human-review hold carries a plain-language note in the session's dashboard chat lineage; holds that don't need the human are cleared, not parked; operator-UI messages get a UI reply. `[mem:needs_you_badge_requires_chat_note; J:2026-06-28]`

## 9. Verification & honesty discipline
- **R9.1 Production is the oracle.** Self-authored synthetic tests are never completion evidence; a real run on the real workload is. `[ORCHESTRATION_INTEGRITY; 6SIGMA_WORKFLOW]`
- **R9.2** End-to-end verification before claiming a fix works: trace every caller + the actual data flow; unit-verifying then declaring done is a violation. `[mem:end_to_end_verification_required, J:2026-05-18]`
- **R9.3** "Done" = evidence (commit SHA + mechanical gate + production observation); honest-incomplete is always acceptable; a false "done" never is. `[ORCHESTRATION_INTEGRITY]`
- **R9.4** Root-cause over patch: a fix that adds a bypass (`if X: continue`) around a broken path must first ask why the path is reachable; the upstream shape is corrected instead wherever possible. First error = full stop + root cause. `[6SIGMA_WORKFLOW; FAMILY_KERNEL behavioral non-negotiables]`
- **R9.5** Confirm an action is right for the STATE, not just that something changed (tree-changed ≠ success; validated advance requires the expected semantic outcome). `[mem:video_complete_is_up_next_present]`

## 10. Knowledge & learning
- **R10.1** Working solutions are never destroyed — they are superseded with history (versioned BTs, supersession chains); regressions must be recoverable by construction. `[J:2026-07-03 "You lose everything positive or destroy it"]`
- **R10.2** Solved screens persist: identity + BT + Q/A capture survive restarts and sessions; the system never re-derives what it already solved. `[J:2026-07-03/07-09]`
- **R10.3** Platform knowledge files stay human-readable and canonical on disk; any DB layer over them is a rebuildable index, never the source of truth. `[taey-ed-state-context p1-schema-design]`
- **R10.4** Learned observations feed future prompts for the same screen type; learning writes are bounded and summarized, never unbounded growth. `[CLAUDE.md learning loop]` *(NOTE: `learned/` currently write-only dead — pending Jesse decision: revive the read path or delete. See inventory.)*

## 11. Open items awaiting ruling
- **O1** `learned/` store: revive read path vs delete (R10.4 note). `[inventory 1.6]`
- **O2** Worker direct-solves vs prescribed find_all→send_to_llm→for_each recipe shape (recipe-design question). `[mem:worker_bt_shape_variance OPEN]`
- **O3** Legacy transition variant_bts consolidation. `[mem:transition_deterministic_serve_cluster OPEN]`
- **O4** `spark_v2/` in the codex worktree: generated debris — deletion CONFIRMED (removed-as-unused per commit 2c82ab0), executing in cleanup-repo-hygiene. `[codex step-0 report 2026-07-09]`
- **O5** Abandon-clear hash-key mismatch: `/abandon_consultation` clears escalation state by `compute_tree_hash` while the live ladder keys by `skeleton_hash` — the per-consult user-Stop reset is likely a NO-OP (masked by `/session/reset`). Fix in p2 coordination migration. `[escalation reconstruction F1]`
- **O6** Dormant duplicate ladder (`consultation_request.py:255-431`, retries.txt path) bypasses the monotonic store if ever re-reached; no repo caller sets its trigger. Delete in p2 flag-machine migration. `[F2]`
- **O7** CLAUDE.md "Escalation Model" section is two generations stale (3-failed-consults / spark_attempts≥2 → superseded by one-shot + 4-tier ladder); "Gemini decides"/Gemini-primary wording predates the Claude worker cutover. Rewrite CLAUDE.md against this canon. `[F3; git mine]`
- **O8** Empty-consult-id escalations (stuck/UNKNOWN/user-message) bypass note_attempt dedup — each resume-cycle failure legitimately climbs; bounded but imperfect. Model properly in the p2 coordination schema. `[F4]`
- **O9** No code path extends `resume_at` while research is in flight — a DR outlasting the 20-min window can climb the ladder indirectly through real re-attempt failures. Design a response-ready-gated resume in p2. `[F5]`
