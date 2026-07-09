# Taey-Ed REQUIREMENTS — The Canon
**Status: DRAFT v0 (2026-07-09)** — curated by the supervisor from standing directives; pending (1) merge of the verbatim evidence sweep (`dispatches/2026-07-09_requirements_evidence_sweep.md`) and (2) Jesse's ratification. Once ratified, this document is **the contract**: every phase of `taey-ed-state-context` (and any future change) is validated against it. Conflicts between code and this canon are bugs in the code.

Sources are cited per rule: `[CLAUDE.md]` = project instructions; `[mem:<slug>]` = project memory; `[yaml:<screen_type>]` = screen-type recipe contract; `[J:<date>]` = direct Jesse directive.

---

## 0. Mission & scale
- **R0.1** The system autonomously completes courses on educational platforms: navigate, watch, read, solve, advance — with minimal user action. `[CLAUDE.md]`
- **R0.2 SCALE:** must support **100s–1000s of platforms, figured out dynamically**. Zero per-platform code anywhere in the flow; a new platform costs one knowledge-research cycle, no code or schema changes. `[J:2026-07-09]`
- **R0.3** All intelligence is server-side and platform-generic: the LLM classifies, the LLM builds behavior trees, recipes + knowledge are data, never code. ("Gemini decides" — no hardcoded screen types, no platform-specific logic in the flow.) `[CLAUDE.md Cardinal Rule 1]`
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

## 8. Retry / escalation / operator discipline
> **INCOMPLETE (J:2026-07-09): "The escalation process is completely missing from this requirements file."** The full AS-BUILT escalation process (tier ladder, triggers, consultation lifecycle, diagnosing/resume semantics, image escalation tiers, Chats/DR dispatch, terminal/give-up, user_input_needed) is being reconstructed by a dedicated fleet discovery pass and will replace this section. The rules below are fragments, not the process.
- **R8.1** One retry with failure context, then stop and ask — never loop the same failing BT. `[CLAUDE.md Escalation Model]`
- **R8.2** Escalation state is monotonic and sticky-terminal; it is cleared only by user-Stop, genuine screen advance, or a recipe (YAML) change — never by manually deleting flags. `[escalation_state.py header, J:2026-06-14]`
- **R8.3** Every human-review hold carries a plain-language note in the session's dashboard chat lineage; holds that don't need the human are cleared, not parked. Operator-UI messages get a UI reply. `[mem:needs_you_badge_requires_chat_note, J:2026-06-28]`
- **R8.4** Wait-on-operator is response-driven, not timer-guessed: resume when the response is ready, not sooner or later. `[J:2026-06 resume directive]`
- **R8.5** Knowledge gate: a platform with no knowledge.json BLOCKS (research is requested once, flagged, waited) — the model never builds blind on an unknown platform. `[CLAUDE.md knowledge gate]`

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
- **O4** `spark_v2/` in the codex worktree: generated debris (504K pycache + provisional_knowledge.json, no source) — delete after Jesse/codex confirm nothing unlanded. `[codex step-0 report 2026-07-09]`
