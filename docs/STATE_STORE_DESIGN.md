# Taey-Ed State Store — Design
**Status: DRAFT v0 (2026-07-09)** — decisions locked against grok's LOGOS refutation (dispatches/2026-07-09_grok_state_store_validation_REPORT.md), the migration surface inventory (dispatches/2026-07-09_migration_surface_inventory.md), and docs/REQUIREMENTS.md. Full DDL lands with `taey-ed-state-context::p1-schema-design`. Every grok verdict gets a design decision below — none are waved off.

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
- `knowledge_index` — file-canonical YAML/JSON stays on disk (R10.6; mtime caches stay VALID — *partially answers grok Claim 5*); the DB indexes `(path, source_sha256, ingested_at)` + provisional/trusted state per piece with validated-success counters (R10.5).

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
1. `coordination.resume_at` + response-ready-gated resume (canon O9): add `response_pending_until`/extension on DR dispatch so an in-flight research response holds the dead-man window?
2. events retention/pruning policy (append-only forever vs archive table past N days) — no silent loss, explicit archive.
3. Whether `screen_sessions` (working memory) moves in Phase A or stays file-backed until p4 (its render-budget semantics are the most write-amplified — grok Claim 2 evidence).
