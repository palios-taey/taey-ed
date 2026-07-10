# DB Design Brief — Full Context for Reviewers
**2026-07-10 · taey-ed Supervisor.** This is the complete context packet for the state-store design review. Read the actual artifacts — `spark/state_schema.sql`, `docs/STATE_STORE_DESIGN.md`, `docs/REQUIREMENTS.md` — and reach your own conclusions; this brief exists so no reviewer lacks the operational history behind them.

## 1. Settled: "just use JSON" is FALSIFIED by production history
Prior reviews suggested flat JSON was sufficient. The production record says otherwise, with receipts:
- The system ran on 7 hand-synchronized JSON/file stores + `/tmp` flag files. Documented consequences: a failed classification **cached as permanent success** (screens trapped UNKNOWN); a poisoned per-screen JSON making the worker repeat rejected plans; **one content-hash mapping to six different screen types** (hash-as-identity); a `/tmp` flag re-arm that produced 62 rebuilds of one screen (a severe runaway-cost incident); torn reads from non-atomic writes under a 0.5s poll; three in-memory dicts silently losing state on every restart. Every one is cited to file:line in `dispatches` inventories and `docs/REQUIREMENTS.md` §11-12 open items.
- On 2026-07-09, live: one exercise looped **466 times** because a cached type verdict, a conformance check, and escalation flag state could not be reconciled across three separate stores. The founder's verdict: the blobs do not work; a real store is required **now**, and it is required **anyway** for the scale target.
- This question is closed. The review question is *whether THIS design is the right store* — not whether a store is needed.

## 2. The scale target (constitutional, REQUIREMENTS R0.2)
100s–1000s of course platforms, onboarded dynamically (one research cycle, zero code, zero schema change). Screen-type mappings must be **durable**: mapped once → recognized deterministically (signature-only) forever; only genuinely novel screens reach a classifier. BTs are then built automatically by an LLM worker whose per-call context is **exactly scoped** (leveled L0–L4 JIT injection with receipts) — no noise, no dumps, fail-closed when a required slice is missing.

## 3. The full stack IS on the table — do not shy from it
This project's operators run production vector-database, graph-database, and local high-dimensional embedding infrastructure at scale. Complexity is not a constraint here. The SQLite choice in the current design is for the *hot transactional state path* (0.5–3s Mac polls, two writer daemons, single host) — it is NOT a capability ceiling. Reviewers should explicitly evaluate:
- Should **semantic screen similarity** (novel screen → "nearest mapped type" candidate ranking) use the embedding stack rather than exact feature signatures alone? (Cross-platform type transfer: a dropdown on platform #47 resembles dropdowns on platforms #1-46.)
- Should the **L0–L3 context slices / knowledge lifecycle** live in or sync to the graph/vector layer for retrieval-grade access (a sibling system already runs a Type-2 SCD property graph in production)?
- Should **qa_captures / extracted course content** flow into the RAG layer (they are embeddings-bound anyway via the user's local KB)?
- Or is the right shape SQLite-for-state + explicit sync jobs into Weaviate/Neo4j for the semantic/analytic layers? Hybrid answers are welcome; "SQLite alone forever" and "one giant JSON" both need justification against the record above.
Constraint that stands regardless: the **user's KB (content+vectors) never leaves their Mac** (sqlite+faiss local; wire contract fixed).

## 4. The hard constraints (non-negotiable, from production)
- Mac polls 0.5–3s and must never block; the Mac is FROZEN (dumb primitives; never touches any server store).
- Two writer daemons (API + worker) + an operator session; single host today.
- No silent truncation anywhere; failure must be representable but never servable as success; escalation ladder state monotonic + sticky-terminal with authorized-clear-only (now enforced as store triggers — try to break them).
- Files stay canonical for authored knowledge (YAML/knowledge.json); any DB layer over them is a rebuildable index.
- Billing ledger stays isolated (separate DB file).

## 5. What already survived adversarial review
- LOGOS round 1: 6 refutations → answered decision-by-decision (aggregates-vs-events on the hot path, dual-write shadow cutover, resolver spec, blast-radius isolation).
- LOGOS round 2 (against the committed DDL): GO-WITH-CHANGES; all 5 required changes now in the schema (retention archive-before-delete enforced by trigger, composite indexes, full trigger audit incl. clear-path guards, WAL monitoring in the shadow phase, receipt hashing for provenance audit).
- Open items O5–O9 (escalation defects) are fixed by construction in the schema — reviewers should verify that claim, not accept it.

## 6. What a review must deliver
Findings + verdict (GO / GO-WITH-CHANGES / NO-GO) on: schema shape & indexing at 1000-platform scale; the invariants-as-triggers approach; the dual-write cutover; the §3 stack question (where do vector/graph layers genuinely earn their place vs add moving parts); anything in production that the authors haven't seen. Cite files. Do not trust this brief where it conflicts with the artifacts — the repo is the oracle.
