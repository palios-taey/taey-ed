# Taey-Ed State Store — Design
**Status: v1 (2026-07-10)** — decisions locked against grok's LOGOS refutation (dispatches/2026-07-09_grok_state_store_validation_REPORT.md), the migration surface inventory (dispatches/2026-07-09_migration_surface_inventory.md), and docs/REQUIREMENTS.md. **Formal DDL: `spark/state_schema.sql`** (tables + CHECK enums + monotonicity/stickiness/append-only TRIGGERS — invariants live at the store, R8.3). §8-§9 added per Jesse 2026-07-10: scaling + leveled JIT context + durable mappings are first-class design goals, not afterthoughts.

## 0. Regime honesty (what kind of system this is)
The Mac polls every 0.5–3s and must NEVER block (R7.6, grok premise-invalidator #1). DCM recomputes verdicts on read because its reads are rare; taey-ed's reads are constant. Therefore:

**D0. Aggregates-as-rows + events-as-audit — NOT pure event-sourcing on the hot path.** *(answers grok Claim 2)*
- Hot-path verdicts read narrow, indexed, single-row aggregates: current BT = one `behavior_trees` row by `(screen_id, status)`; escalation tier = one `coordination` row; classification = one `screens.classification_status` enum.
- The append-only `events` table is the audit trail + RCA substrate + failure history — read on escalation/diagnosis paths (rare), not per poll.
- The no-cached-failure invariant survives where it matters: status ENUMs make "failed" a first-class, never-terminal, never-served-as-success state (R2.5); a verdict row can only reach `validated` via the evidence-gated transition, and demotion is a transition, not a delete.

## 1. Two database files, not one *(answers grok Risk 5)*
- `taey_ed.db` — users / refresh_tokens / credit_ledger. UNTOUCHED. Billing never shares a WAL with screen state.
- `taey_state.db` — everything below. Own WAL, own checkpoint cadence.

## 2. Concurrency discipline *(answers grok Claim 1)*
- `PRAGMA journal_mode=WAL` (persistent, set in init), `busy_timeout=5000`, `synchronous=NORMAL`.
- `init_state_db()` called from BOTH entrypoints (server.py AND worker/run.py — worker imports nothing from db.py today; inventory Part 4).
- Every read-modify-write is `BEGIN IMMEDIATE` (conflicts surface at BEGIN as retryable BUSY, not mid-txn upgrade deadlocks). Single-statement appends (events) need no explicit txn.
- Intra-process: per-call connections stay; the module Lock stays as a thread-safety device, NOT the correctness mechanism.
- Writer pressure is structurally low: a poll writes ≤2 rows (an event append + at most one aggregate transition). The worker writes on consult completion only.
- **Proof is production, not synthetic load** (NO-TESTS rule): the dual-write shadow phase (§6) measures real writer-wait/latency under real Mac polling before anything depends on the DB.

## 3. Schema (tables + the invariants they enforce)
- `screens` — minted `screen_id` (uuid), platform, screen_type/subtype (NEVER a bare master — CHECK, R2.7), `classification_status` ENUM(pending/classified/failed_retryable/operator_required) — failure is representable and never terminal (R2.5), `question_fingerprint`, timestamps.
- `screen_keys` — `(key_kind[skeleton|signature|widget_set], key_hash) → screen_id`, many-to-many. Collisions are ROWS, not fusions.
- `screen_features` — per screen: uncapped widget presence-set, discriminative marker set, question fingerprint. The resolver's disambiguation substrate *(answers grok Claim 3 — see §4)*.
- `behavior_trees` — versioned rows: `(screen_id, revision)`, bt_json, provenance (built_by, knowledge_sha, source), status ENUM(candidate/validated/demoted/retired/rejected), `supersedes`, validation evidence + success_count. R10.1: supersede, never destroy. Promotion at 3 validated successes, demotion at 2 consecutive failures = transitions with recorded cause (R10.5; INTENDED_FLOW §E).
- `qa_captures` — screen_id, question verbatim, options, chosen answer, correctness, kb_chunks_served ref. Questions stored as CONTENT, never hashes (R3.3, R3.14).
- `events` — append-only: kind, screen_id, consult_id, payload_json, actor, created_at. Indexed (screen_id, kind, created_at DESC). Audit + RCA + the R3.13 served-context record (with `bundle_receipts`).
- `coordination` — ONE row per (platform, screen_id) escalation gate, modeling the FULL /tmp semantics enumerated by grok *(answers Claim 4, not a naive mapping)*:
  - `state` ENUM(normal/consulting/diagnosing/awaiting_resume/terminal/cleared), `tier`, `attempt_count` (monotonic — trigger blocks decrement), `terminal` sticky (trigger blocks unset except via the three authorized clears: user-stop / advance / — fold does NOT clear terminal, R8.5),
  - `last_failed_consult_id` (climb dedup, R8.4), `resume_at`, `notify_cycle_id` + `notify_emitted_at` (once-per-cycle notify becomes exactly-once by transaction, replacing flag-presence), `chat_dispatched_tiers` (per-{screen,tier} dedup, R8.12), `yaml_mtime_at_attempt` (fold detection, R8.5),
  - Artifact BLOBS (tree/screenshot always-overwrite dumps, packets, user_instructions) STAY AS FILES referenced by path — the DB owns TRANSITIONS, the filesystem owns bytes (inventory Part 4 recommendation). Operator file-pokes get CLI/API replacements (gave-up, user-instructions, consult respond).
- `consults` — the /tmp/taey-ed-consult state machine: status ENUM(pending/complete/worker_failed/abandoned) + TTL fields + reason; payload blobs stay files. Closes the O5 hash-key-mismatch defect (keyed by screen_id, not two different hash functions) and deletes the O6 dormant duplicate ladder.
- `context_slices` (formerly drafted as `knowledge_index` — renamed in the DDL; GAIA + the independent auditor both caught the stale name here) — file-canonical YAML/JSON stays on disk (R10.6; mtime caches stay VALID — *partially answers grok Claim 5*); the DB indexes `(platform, level, selector, source_sha)` + provisional/trusted state per piece with validated-success counters (R10.5). Table count note: the schema is **14 tables** including `events_archive` (added by LOGOS req #1); `screen_sessions` (L4 working memory) is deliberately NOT in the schema yet — open question §7.3 decides whether it migrates in Phase A or stays file-backed until p4.

## 4. The resolver, specified *(answers grok Claim 3)*
On every capture: compute skeleton_hash + signature markers + UNCAPPED widget presence-set + question_fingerprint (the max_depth=15 truncation is fixed AT THE SOURCE in p3 — the uncapped walk becomes part of feature extraction, not a bolt-on).
1. Candidate set = screen_keys lookup (all key kinds).
2. 0 candidates → MINT new screen_id + store features. Never guess.
3. 1 candidate → verify feature equality (widget set, markers). Match → resolved. Mismatch → MINT + record an `ambiguity` event (collision made visible, R2.6).
4. >1 candidates → disambiguate in order: widget-set equality → question_fingerprint → marker set. Unique survivor → resolved. No unique survivor → MINT + `ambiguity` event + operator flag (R10.7: multiple matches flag the operator to tighten). **Never fuse, never silently pick.**
5. Genuinely identical feature vectors (shared quiz skeleton): same screen TYPE; instance distinguished by question_fingerprint; where even that matches, the live tree is the authority on every encounter (R2.3) — no stored per-instance answer reuse.

## 5. What is NOT consolidated *(answers grok premise-invalidator #2)*
- The credit ledger (own DB, §1).
- knowledge YAML/JSON files (canonical on disk; DB is a rebuildable index).
- Binary artifacts (screenshots/trees/packets — files, DB holds paths + hashes).
- The Mac's local KB (sqlite+faiss on the user's Mac — R1.2, never touched).

## 6. Cutover: dual-write shadow, then flip *(answers grok Claims 5, 6 + Risks 1, 2, 4)*
- **Phase A (shadow):** adapters dual-write file + DB; ALL reads still from files. Production traffic populates the DB. Zero behavior risk; real-load latency + writer-wait measured on the REAL poll cadence (the production oracle, not synthetic tests).
- **Phase B (parity gate):** delivery gate = N real runs with zero file/DB divergence (mechanical diff of both stores after each run) + DB write-latency bounds observed. Grok's "what would make Claim 1/2 true" evidence is produced here or the flip doesn't happen.
- **Phase C (flip):** reads move to the DB store-by-store (signatures → variant/hash → sessions → escalation → consult gate LAST, since it owns the wait-timing semantics grok flagged in Risk 2). Files become the shadow for one more gate, then retire.
- **Importer** is only for DURABLE backfill (signatures, variant_bts, hash_index, session archives, escalation_state) run in a quiesced window; /tmp in-flight state is NEVER imported — it drains via TTL (600s) before Phase C. Idempotency via natural keys + source sha. "Lossless" is claimed only for durable state; ephemeral in-flight state is explicitly out of scope *(concedes grok Claim 6's live-state point)*.

## 7. Open design questions (for Chats DB review + grok round 2)
1. ~~response-ready-gated resume~~ DECIDED: `coordination.response_pending_until` — a live DR/Family dispatch extends the dead-man window; the timer never steamrolls in-flight research (fixes O9).
2. events retention/pruning policy (append-only forever vs archive table past N days) — no silent loss, explicit archive.
3. Whether `screen_sessions` (working memory) moves in Phase A or stays file-backed until p4 (its render-budget semantics are the most write-amplified — grok Claim 2 evidence).

## 7a. Grok round-2 answers (2026-07-10 — every refutation gets a decision)
- **#5 trigger bypass (REFUTED → FIXED):** the bare `cleared`-state escape allowed any writer to clear-then-decrement. Now: transitioning to `cleared` ABORTS without an authorized `cleared_reason`; `yaml_fold` can clear the ladder but can NEVER un-terminate; every clear is an events row. Executed grok's exact bypass against the amended DDL — ABORTS. True who-verification is the app-layer single clear chokepoint (p2 acceptance item).
- **#2 max_depth=15 still live in source (CONFIRMED — deliberate sequencing, not an oversight):** changing skeleton hashing NOW would invalidate every live `hash_index`/signature key mid-production-run for zero gain. The store is DESIGNED to make that fix safe: hashes are demoted to lookup attributes (`screen_keys`), identity is minted — so when the uncapped feature walk lands in p3, old hashes keep resolving (rows, not identity) and re-keying is a non-event. Sequence: store first (keys tolerated), THEN fix the walk where invalidation costs nothing. The dual-write phase measures the OLD keys against the store precisely so p3 has a baseline. If p3 slips, this becomes the top risk — it is `depends:`-encoded so it cannot silently slip.
- **#1/#3/#4 spec-vs-implementation gaps (CONFIRMED):** adapters, signature-registration, and JIT enforcement do not exist at this commit — they ARE p2/p4. These verdicts become the p2/p4 delivery-gate checklist: parity evidence for adapters (Claim 1/2), signature-only resolution demonstrated on live traffic (Claim 3), receipts on every call (Claim 4).

## 8. Durable mappings — map once, NEVER redo (Jesse 2026-07-10)
The point of identity is that recognition work is never repeated:
- **TYPE grain** (`screen_types` + `type_signatures`): the first time a screen type is mapped on a platform, its feature signatures (uncapped widget-presence set, discriminative markers, dom-classes) are registered. Every later instance of that type matches **deterministically by signature — no LLM call, no re-classification, no re-mapping.** Only a genuinely novel feature signature ever reaches the classifier. The type's YAML is authored once and improved in place (R10.6); its trust/promotion state (`provisional → 3 validated successes → trusted`, demote-on-failure) lives on the registry row (R10.5).
- **INSTANCE grain** (`screens` + `screen_keys` + `screen_features`): minted `screen_id`, many-to-many lookup keys, collisions representable (rows, never fusions). Deterministic types replay their validated BT (`behavior_trees.status='validated'`); dynamic types rebuild fresh from the type recipe — but the RECOGNITION is never redone.
- **BT auto-build path (the steady state):** capture → resolver (signatures → screen_id + type, deterministic) → JIT bundle (§9) → worker builds the BT from THE ONE recipe with on-point context → conformance → execute → validate → promotion counters. Once a platform's types are mapped, screens flow through with zero classification cost and zero operator involvement; the worker builds BTs automatically with instructions that are precise every time.

## 9. Leveled JIT context injection (Jesse 2026-07-10: "no confusing noise")
Context is a 5-level hierarchy; every LLM call gets a **bounded recipe of slices — one per level, exactly scoped** — assembled from `context_slices` (L0-L3 index over the canonical files) + live screen state (L4). Never a dump; every call writes a `bundle_receipts` row (what was served, what was dropped and WHY — R7.1/R7.5).

| Level | Scope | Content | Store |
|---|---|---|---|
| L0 | universal | core BT rules, handler contracts, universal exercise pattern | `context_slices(platform=NULL, level=0)` |
| L1 | platform | knowledge.json: timing, never_click, quirks(-for-this-category only) | `context_slices(level=1)` |
| L2 | category | MASTER routing context (optional, small) | `context_slices(level=2)` |
| L3 | screen type | THE ONE YAML (recipe/contracts/actuation/verification) | `context_slices(level=3)` |
| L4 | screen instance | screen session (attempts/lessons for THIS screen), qa history, KB chunks (exercise family only) | `screens`/`qa_captures`/session |

Call recipes (per `bundle_receipts.call_kind`): **classify** = L0-lite + L1 classification guide + registered-type list (small — and rare, per §8); **bt_build** = L0 core + L1 quirks-for-category + L3 full + L4 session (+ KB chunks if exercise); **retry_build** = bt_build + the failure context (failed BT + verdict); **diagnose** (escalation packet) = everything above + full attempt history. Required-slice-empty ⇒ refuse and escalate (fail-closed, R7.4). Budget pressure drops WHOLE optional slices with a receipt — never mid-content cuts (R7.1). This is the worker-confinement contract (R7.9/R7.10) made mechanical: core + THE ONE YAML + THIS screen's state, nothing else, receipted.

## 10. Family panel verdict + the stack ruling (2026-07-10, all four lanes)
LOGOS GO-WITH-CHANGES · COSMOS GO-WITH-CHANGES ("fundamentally sound") · GAIA GO-WITH-CHANGES (+ SQL corrections artifact, DDL loaded into real SQLite) · HORIZON **NO-GO against the v1 DDL** ("false sense of fixed-by-construction — several invariants break with ordinary SQL"). GAIA + HORIZON independently proved the same enforcement gaps. **Schema v2 applies the full correction set as shapes** (non-enforcing NULL-in-IN CHECKs rebuilt; cleared-state immutability; DELETE/REPLACE guards; faithful-copy archive + id retirement; tier monotonic + attempt ceiling; unforgeable classification (receipt FK) and validated-BTs (ux_one_validated + success floor); slice SCD currency + content-addressed slice_blobs so receipts stay verifiable after in-place YAML edits; ms-INTEGER timestamps, events ordered by event_id; dedup as PK-conflict tables — never JSON columns; ONE-pending-consult as a uniqueness constraint). Verified: 19/19 cross-lane attacks blocked, executed not assumed. HORIZON re-verdict on v2 requested before p2 build.

**§3 stack ruling (HORIZON's 5-plane answer, adopted):** SQLite = authoritative single-host transactional state plane · content-addressed immutable files = audit/artifact plane · vector layer = cross-platform semantic candidate plane (novel screen → nearest mapped type; p3+) · graph layer = selective ontology/provenance plane (selective, not default) · the user's local sqlite+faiss KB = a completely separate course-content plane that never enters fleet RAG.
