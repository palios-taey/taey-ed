-- ============================================================================
-- taey-ed STATE STORE — formal DDL (taey_state.db, separate from taey_ed.db)
-- Design: docs/STATE_STORE_DESIGN.md · Contract: docs/REQUIREMENTS.md
-- SQLite, WAL mode. Init sets: journal_mode=WAL, busy_timeout=5000,
-- synchronous=NORMAL, foreign_keys=ON. All RMW transactions BEGIN IMMEDIATE.
-- Writers: API daemon + worker daemon (+ operator via CLI/API, never raw files).
-- The Mac NEVER touches this DB (HTTP only).
--
-- SCALING INVARIANTS (Jesse 2026-07-09/10):
--  * Every row is platform-scoped: 100s-1000s of platforms, zero per-platform
--    code, no schema change to onboard a platform (one knowledge-research row).
--  * MAPPINGS ARE DURABLE: a screen type mapped once is recognized forever
--    (feature signatures at TYPE grain; minted IDs at INSTANCE grain). Only
--    genuinely novel screens ever reach the classifier.
--  * JIT LEVELED CONTEXT: context lives in 5 levels (L0 universal, L1 platform,
--    L2 category, L3 screen-type, L4 screen-instance). Every LLM call gets
--    exactly one bounded slice-set per its recipe — never a dump, never noise —
--    and writes a receipt of exactly what was served and what was dropped.
--  * Files stay canonical for authored knowledge (YAML/JSON on disk, R10.6);
--    this DB is identity + state + index + audit. Binary artifacts stay files.
-- ============================================================================

-- ---------------------------------------------------------------------------
-- PLATFORMS — onboarding is a ROW, not code (R0.2)
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS platforms (
    platform        TEXT PRIMARY KEY,             -- 'khan_academy'
    display_name    TEXT,
    onboarded_at    TEXT NOT NULL DEFAULT (datetime('now')),
    knowledge_path  TEXT,                         -- canonical knowledge.json on disk
    knowledge_sha   TEXT,                         -- sha256 of last ingested version
    status          TEXT NOT NULL DEFAULT 'active'
                    CHECK (status IN ('researching','active','paused'))
);

-- ---------------------------------------------------------------------------
-- SCREEN TYPE REGISTRY — the DURABLE TYPE MAPPING (map once, never redo)
-- One row per (platform, screen_type). The YAML on disk stays the authored
-- truth (R10.6); this row is its identity + promotion state + index.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS screen_types (
    platform        TEXT NOT NULL REFERENCES platforms(platform),
    screen_type     TEXT NOT NULL,                -- 'EXERCISE_DROPDOWN' — NEVER a bare master (R2.7)
    category        TEXT NOT NULL,                -- master: NAVIGATION/VIDEO/ARTICLE/EXERCISE/TRANSITION
    artifact_path   TEXT NOT NULL,                -- screen_types/<TYPE>.yaml
    artifact_sha    TEXT NOT NULL,                -- sha256 at last ingest (fold detection = sha change, not mtime)
    deterministic   INTEGER NOT NULL DEFAULT 0,   -- R2.2: VIDEO/ARTICLE fixed-BT; NAV shared-BT; EXERCISE recipe
    trust           TEXT NOT NULL DEFAULT 'provisional'
                    CHECK (trust IN ('provisional','trusted','demoted')),
    validated_successes INTEGER NOT NULL DEFAULT 0,  -- R10.5: 3 promote; demote on failure
    ingested_at     TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (platform, screen_type),
    CHECK (screen_type <> category)               -- bare-master ban, enforced at the store
);

-- TYPE-GRAIN feature signatures: recognize "another instance of a KNOWN type"
-- without any LLM. Features are extracted from the FULL tree (uncapped widget
-- presence set, discriminative markers) — the resolver's first gate.
CREATE TABLE IF NOT EXISTS type_signatures (
    platform        TEXT NOT NULL,
    screen_type     TEXT NOT NULL,
    sig_kind        TEXT NOT NULL CHECK (sig_kind IN ('widget_set','marker_set','dom_class')),
    sig_value       TEXT NOT NULL,                -- canonical serialized feature set
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (platform, sig_kind, sig_value, screen_type),
    FOREIGN KEY (platform, screen_type) REFERENCES screen_types(platform, screen_type)
);

-- ---------------------------------------------------------------------------
-- SCREENS — INSTANCE identity. Minted IDs, never content-hash-as-identity (R2.6).
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS screens (
    screen_id       TEXT PRIMARY KEY,             -- minted uuid
    platform        TEXT NOT NULL REFERENCES platforms(platform),
    screen_type     TEXT,                         -- resolved type (NULL until classified)
    classification  TEXT NOT NULL DEFAULT 'pending'
                    CHECK (classification IN ('pending','classified','failed_retryable','operator_required')),
                    -- 'failed' is REPRESENTABLE and NEVER terminal/served-as-success (R2.5)
    question_fingerprint TEXT,                    -- instance discriminator on shared skeletons
    first_seen      TEXT NOT NULL DEFAULT (datetime('now')),
    last_seen       TEXT NOT NULL DEFAULT (datetime('now')),
    retired         INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_screens_platform_type ON screens(platform, screen_type);

-- INSTANCE lookup keys: many-to-many — a colliding hash is ROWS, never a fusion.
CREATE TABLE IF NOT EXISTS screen_keys (
    platform        TEXT NOT NULL,
    key_kind        TEXT NOT NULL CHECK (key_kind IN ('skeleton','signature','widget_set')),
    key_hash        TEXT NOT NULL,
    screen_id       TEXT NOT NULL REFERENCES screens(screen_id),
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (platform, key_kind, key_hash, screen_id)
);
CREATE INDEX IF NOT EXISTS idx_screen_keys_lookup ON screen_keys(platform, key_kind, key_hash);

-- Per-screen extracted features (resolver disambiguation substrate; R2.6 —
-- never fuse, never guess: 0 matches -> mint; ambiguous -> mint + ambiguity event).
CREATE TABLE IF NOT EXISTS screen_features (
    screen_id       TEXT NOT NULL REFERENCES screens(screen_id),
    feature_kind    TEXT NOT NULL,                -- 'widget_set' | 'marker_set' | 'dom_classes' | ...
    feature_value   TEXT NOT NULL,
    PRIMARY KEY (screen_id, feature_kind)
);

-- ---------------------------------------------------------------------------
-- BEHAVIOR TREES — versioned, supersede-never-destroy (R10.1); auto-build path
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS behavior_trees (
    bt_id           TEXT PRIMARY KEY,             -- minted uuid
    screen_id       TEXT NOT NULL REFERENCES screens(screen_id),
    revision        INTEGER NOT NULL,
    bt_json         TEXT NOT NULL,
    built_by        TEXT NOT NULL,                -- 'worker' | 'fixed_recipe' | 'operator-era-legacy'
    source_kind     TEXT NOT NULL,                -- 'recipe' | 'unknown_guide' | 'fixed'
    type_artifact_sha TEXT,                       -- YAML sha the build saw (fold lineage)
    bundle_id       TEXT,                         -- context bundle that produced it (receipt link)
    status          TEXT NOT NULL DEFAULT 'candidate'
                    CHECK (status IN ('candidate','validated','demoted','retired','rejected')),
    success_count   INTEGER NOT NULL DEFAULT 0,
    consecutive_failures INTEGER NOT NULL DEFAULT 0,   -- demote at 2, never delete on one-off
    supersedes      TEXT REFERENCES behavior_trees(bt_id),
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (screen_id, revision)
);
CREATE INDEX IF NOT EXISTS idx_bt_current ON behavior_trees(screen_id, status);

-- ---------------------------------------------------------------------------
-- QA CAPTURES — question context first-class (R3.3/R3.14): content, never hashes
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS qa_captures (
    qa_id           TEXT PRIMARY KEY,
    screen_id       TEXT NOT NULL REFERENCES screens(screen_id),
    question        TEXT NOT NULL,                -- verbatim
    options_json    TEXT,                         -- enumerated options (R3.4)
    answer          TEXT,
    correctness     TEXT CHECK (correctness IN (NULL,'correct','wrong','unsubmitted')),
    kb_chunks_ref   TEXT,                         -- what grounding was served (R3.9/R3.13)
    captured_at     TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_qa_screen ON qa_captures(screen_id);

-- ---------------------------------------------------------------------------
-- CONTEXT SLICES — the JIT LEVELED CONTEXT index (L0-L3; L4 is screen state)
-- Files stay canonical; rows are the queryable index + trust state. The
-- assembler pulls BY (platform, level, selector) — bounded, never a dump.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS context_slices (
    slice_id        TEXT PRIMARY KEY,
    platform        TEXT,                         -- NULL for L0 universal
    level           INTEGER NOT NULL CHECK (level IN (0,1,2,3)),
                    -- 0 universal core rules / handler contracts
                    -- 1 platform knowledge (timing, never_click, quirks)
                    -- 2 category shared context (MASTER routing)
                    -- 3 screen-type artifact (THE ONE YAML)
    selector        TEXT NOT NULL,                -- e.g. 'core_rules', 'quirks:EXERCISE', 'EXERCISE_DROPDOWN'
    source_path     TEXT NOT NULL,
    source_sha      TEXT NOT NULL,
    trust           TEXT NOT NULL DEFAULT 'trusted'
                    CHECK (trust IN ('provisional','trusted','superseded')),
    verified_count  INTEGER NOT NULL DEFAULT 0,
    ingested_at     TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (platform, level, selector, source_sha)
);
CREATE INDEX IF NOT EXISTS idx_slices_lookup ON context_slices(platform, level, selector, trust);

-- Every LLM call's served context — the RECEIPT (R7.5): exactly what was
-- included, what was dropped and why. No silent truncation, ever (R7.1).
CREATE TABLE IF NOT EXISTS bundle_receipts (
    bundle_id       TEXT PRIMARY KEY,
    call_kind       TEXT NOT NULL CHECK (call_kind IN ('classify','bt_build','retry_build','diagnose','extract')),
    screen_id       TEXT,
    slices_json     TEXT NOT NULL,                -- [{slice_id|l4 ref, chars}...] exact set served
    dropped_json    TEXT NOT NULL DEFAULT '[]',   -- [{what, why}] — explicit, or empty
    kb_chunks_json  TEXT,                         -- grounding chunks included (exercise family only)
    total_chars     INTEGER NOT NULL,
    created_at      TEXT NOT NULL DEFAULT (datetime('now'))
);

-- ---------------------------------------------------------------------------
-- COORDINATION — the escalation state machine (replaces /tmp flags + files).
-- Fixes by construction: O5 (keys on screen_id — ONE identity, no dual-hash
-- mismatch), O6 (this IS the only ladder), O8 (attempt dedup key), O9
-- (response_pending_until extends the dead-man window while research is live).
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS coordination (
    screen_id       TEXT PRIMARY KEY REFERENCES screens(screen_id),
    state           TEXT NOT NULL DEFAULT 'normal'
                    CHECK (state IN ('normal','consulting','diagnosing','awaiting_resume','terminal','cleared')),
    tier            TEXT CHECK (tier IN (NULL,'tier1','tier2','tier3','terminal')),
    attempt_count   INTEGER NOT NULL DEFAULT 0,   -- MONOTONIC: trigger blocks decrement
    last_attempt_key TEXT,                        -- consult_id OR minted key for consult-less classes (O8)
    terminal        INTEGER NOT NULL DEFAULT 0,   -- STICKY: trigger blocks unset except authorized clears
    resume_at       TEXT,                         -- dead-man window (R8.16)
    response_pending_until TEXT,                  -- O9: DR/Family in flight extends the window
    notify_cycle_id TEXT,                         -- exactly-once notify per cycle (by transaction, not flag)
    dispatched_tiers TEXT NOT NULL DEFAULT '[]',  -- per-{screen,tier} chat-dispatch dedup (R8.12)
    yaml_sha_at_attempt TEXT,                     -- fold detection: sha change after attempt = clean ladder (R8.5)
    user_instructions TEXT,                       -- terminal user-assist (R8.19)
    updated_at      TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Monotonicity + stickiness enforced AT THE STORE (R8.3): no caller — not the
-- operator, not a bug — can decrement the ladder or un-terminate by UPDATE.
CREATE TRIGGER IF NOT EXISTS coordination_monotonic
BEFORE UPDATE ON coordination
WHEN NEW.attempt_count < OLD.attempt_count AND NEW.state <> 'cleared'
BEGIN SELECT RAISE(ABORT, 'attempt_count is monotonic (R8.3); only an authorized clear resets it'); END;

CREATE TRIGGER IF NOT EXISTS coordination_sticky_terminal
BEFORE UPDATE ON coordination
WHEN OLD.terminal = 1 AND NEW.terminal = 0 AND NEW.state <> 'cleared'
BEGIN SELECT RAISE(ABORT, 'terminal is sticky (R8.3); only user-stop/advance clears (R8.5)'); END;

-- ---------------------------------------------------------------------------
-- CONSULTS — the consultation lifecycle (replaces /tmp/taey-ed-consult state).
-- Blobs (tree/screenshot/packet) stay FILES referenced by path.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS consults (
    consult_id      TEXT PRIMARY KEY,
    screen_id       TEXT REFERENCES screens(screen_id),
    platform        TEXT NOT NULL,
    status          TEXT NOT NULL DEFAULT 'pending'
                    CHECK (status IN ('pending','complete','worker_failed','abandoned')),
    abandon_reason  TEXT,
    payload_dir     TEXT NOT NULL,                -- artifact files
    failure_reason  TEXT,
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    resolved_at     TEXT
);
CREATE INDEX IF NOT EXISTS idx_consults_open ON consults(platform, status) WHERE status = 'pending';

-- ---------------------------------------------------------------------------
-- EVENTS — append-only audit (no UPDATE path). RCA substrate + R3.13 record.
-- Hot-path verdicts live in the aggregate rows above; events are the history.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS events (
    event_id        INTEGER PRIMARY KEY AUTOINCREMENT,
    kind            TEXT NOT NULL,                -- classify_attempt/classify_result/bt_build/bt_execution/
                                                  -- escalation/advance/wrong_answer/staging/ambiguity/...
    screen_id       TEXT,
    consult_id      TEXT,
    actor           TEXT NOT NULL CHECK (actor IN ('api','worker','mac','operator','supervisor','system')),
    payload_json    TEXT NOT NULL DEFAULT '{}',
    created_at      TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_events_screen ON events(screen_id, kind, created_at DESC);

CREATE TRIGGER IF NOT EXISTS events_append_only
BEFORE UPDATE ON events
BEGIN SELECT RAISE(ABORT, 'events are append-only'); END;
CREATE TRIGGER IF NOT EXISTS events_no_delete
BEFORE DELETE ON events
BEGIN SELECT RAISE(ABORT, 'events are append-only; archive via the archive job, never DELETE'); END;
