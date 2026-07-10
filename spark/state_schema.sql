-- ============================================================================
-- taey-ed STATE STORE — formal DDL v2 (taey_state.db, separate from taey_ed.db)
-- Design: docs/STATE_STORE_DESIGN.md · Contract: docs/REQUIREMENTS.md
-- v2 (2026-07-10): GAIA review corrections applied AS SHAPES, not per-exploit
-- patches (its process critique): non-enforcing NULL-in-IN CHECKs rebuilt (F1);
-- cleared-state immutability (F2); DELETE/REPLACE guards (F3); fold trigger
-- fixed to guard NEW.terminal so the LEGAL fold-clear works (F4); archive
-- integrity — faithful-copy + id-retirement + archive-append-only (F5); tier
-- monotonic + attempt ceiling + terminal⇒state (F6); classification requires a
-- receipt — success unforgeable (F7); registry CHECKs incl. exercise-never-
-- deterministic + trusted-requires-3 (F8/F9); ONE validated BT per screen
-- (F10); context-slice SCD currency + NULL-platform dedup (F11); receipt
-- bodies content-addressed so provenance stays verifiable after in-place YAML
-- edits (F13); millisecond INTEGER timestamps everywhere — events ordered by
-- event_id, never wall-clock (F14); dedup AS INSERT-conflict tables, never
-- JSON-column read-modify-write (F17); ONE pending consult globally as a
-- uniqueness constraint (R8.6); platform-scoped active-coordination index (F19).
--
-- SQLite ≥3.42 (unixepoch subsec), WAL. Init sets: journal_mode=WAL,
-- busy_timeout=5000, synchronous=NORMAL, foreign_keys=ON. RMW = BEGIN IMMEDIATE.
-- Writers: API + worker daemons (+ operator via CLI/API). Mac never touches it.
-- Timestamps: INTEGER ms since epoch; ONE app-layer helper; no exceptions.
-- ============================================================================

CREATE TABLE IF NOT EXISTS platforms (
    platform        TEXT PRIMARY KEY,
    display_name    TEXT,
    onboarded_at    INTEGER NOT NULL DEFAULT (CAST(unixepoch('subsec')*1000 AS INTEGER)),
    knowledge_path  TEXT,
    knowledge_sha   TEXT,
    status          TEXT NOT NULL DEFAULT 'active'
                    CHECK (status IN ('researching','active','paused'))
);

-- ---------------------------------------------------------------------------
-- SCREEN TYPE REGISTRY — durable TYPE mapping (map once, never redo)
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS screen_types (
    platform        TEXT NOT NULL REFERENCES platforms(platform),
    screen_type     TEXT NOT NULL,
    category        TEXT NOT NULL
                    CHECK (category IN ('NAVIGATION','VIDEO','ARTICLE','EXERCISE','TRANSITION')),  -- F8
    artifact_path   TEXT NOT NULL,
    artifact_sha    TEXT NOT NULL,
    deterministic   INTEGER NOT NULL DEFAULT 0,
    trust           TEXT NOT NULL DEFAULT 'provisional'
                    CHECK (trust IN ('provisional','trusted','demoted')),
    validated_successes INTEGER NOT NULL DEFAULT 0,
    ingested_at     INTEGER NOT NULL DEFAULT (CAST(unixepoch('subsec')*1000 AS INTEGER)),
    PRIMARY KEY (platform, screen_type),
    CHECK (screen_type <> category),                              -- bare-master ban
    CHECK (category <> 'EXERCISE' OR deterministic = 0),          -- F9 / R2.2
    CHECK (trust <> 'trusted' OR validated_successes >= 3)        -- F9 / R10.5 (settles O11 floor)
);

CREATE TABLE IF NOT EXISTS type_signatures (
    platform        TEXT NOT NULL,
    screen_type     TEXT NOT NULL,
    sig_kind        TEXT NOT NULL CHECK (sig_kind IN ('widget_set','marker_set','dom_class')),
    sig_value       TEXT NOT NULL,
    created_at      INTEGER NOT NULL DEFAULT (CAST(unixepoch('subsec')*1000 AS INTEGER)),
    PRIMARY KEY (platform, sig_kind, sig_value, screen_type),
    FOREIGN KEY (platform, screen_type) REFERENCES screen_types(platform, screen_type)
);

-- ---------------------------------------------------------------------------
-- SCREENS — INSTANCE identity. Classification is UNFORGEABLE: a 'classified'
-- row requires a real type AND the bundle receipt that produced it (F7 —
-- "you cannot be classified without a receipt showing what you saw").
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS screens (
    screen_id       TEXT PRIMARY KEY,
    platform        TEXT NOT NULL REFERENCES platforms(platform),
    screen_type     TEXT,
    classification  TEXT NOT NULL DEFAULT 'pending'
                    CHECK (classification IN ('pending','classified','failed_retryable','operator_required')),
    classified_by_bundle_id TEXT REFERENCES bundle_receipts(bundle_id),   -- F7
    question_fingerprint TEXT,
    first_seen      INTEGER NOT NULL DEFAULT (CAST(unixepoch('subsec')*1000 AS INTEGER)),
    last_seen       INTEGER NOT NULL DEFAULT (CAST(unixepoch('subsec')*1000 AS INTEGER)),
    retired         INTEGER NOT NULL DEFAULT 0,
    FOREIGN KEY (platform, screen_type) REFERENCES screen_types(platform, screen_type),  -- F7
    CHECK (classification <> 'classified'
           OR (screen_type IS NOT NULL AND classified_by_bundle_id IS NOT NULL))         -- F7
);
CREATE INDEX IF NOT EXISTS idx_screens_platform_type ON screens(platform, screen_type);

CREATE TABLE IF NOT EXISTS screen_keys (
    platform        TEXT NOT NULL,
    key_kind        TEXT NOT NULL CHECK (key_kind IN ('skeleton','signature','widget_set')),
    key_hash        TEXT NOT NULL,
    screen_id       TEXT NOT NULL REFERENCES screens(screen_id),
    created_at      INTEGER NOT NULL DEFAULT (CAST(unixepoch('subsec')*1000 AS INTEGER)),
    PRIMARY KEY (platform, key_kind, key_hash, screen_id)
);
CREATE INDEX IF NOT EXISTS idx_screen_keys_lookup ON screen_keys(platform, key_kind, key_hash);

CREATE TABLE IF NOT EXISTS screen_features (
    screen_id       TEXT NOT NULL REFERENCES screens(screen_id),
    feature_kind    TEXT NOT NULL,
    feature_value   TEXT NOT NULL,
    PRIMARY KEY (screen_id, feature_kind)
);

-- ---------------------------------------------------------------------------
-- BEHAVIOR TREES — versioned, supersede-never-destroy. Exactly ONE validated
-- BT per screen (F10), and a validated BT cannot be a forgery: it must carry
-- at least one REAL success and not be demotion-eligible. (Deliberate
-- deviation from GAIA's literal >=3: the 3-success floor is TYPE trust
-- (screen_types.trust, R10.5); BT replay-validation is first-real-success —
-- as-built semantics. Documented in design §7a.)
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS behavior_trees (
    bt_id           TEXT PRIMARY KEY,
    screen_id       TEXT NOT NULL REFERENCES screens(screen_id),
    revision        INTEGER NOT NULL,
    bt_json         TEXT NOT NULL,
    built_by        TEXT NOT NULL,
    source_kind     TEXT NOT NULL,
    type_artifact_sha TEXT,
    bundle_id       TEXT REFERENCES bundle_receipts(bundle_id),
    status          TEXT NOT NULL DEFAULT 'candidate'
                    CHECK (status IN ('candidate','validated','demoted','retired','rejected')),
    success_count   INTEGER NOT NULL DEFAULT 0,
    consecutive_failures INTEGER NOT NULL DEFAULT 0,
    supersedes      TEXT REFERENCES behavior_trees(bt_id),
    created_at      INTEGER NOT NULL DEFAULT (CAST(unixepoch('subsec')*1000 AS INTEGER)),
    UNIQUE (screen_id, revision),
    CHECK (status <> 'validated' OR success_count >= 1),          -- F10: unforgeable
    CHECK (status <> 'validated' OR consecutive_failures < 2)     -- R10.5 demote-at-2
);
CREATE INDEX IF NOT EXISTS idx_bt_current ON behavior_trees(screen_id, status);
CREATE UNIQUE INDEX IF NOT EXISTS ux_bt_one_validated
  ON behavior_trees(screen_id) WHERE status = 'validated';        -- F10

CREATE TABLE IF NOT EXISTS qa_captures (
    qa_id           TEXT PRIMARY KEY,
    screen_id       TEXT NOT NULL REFERENCES screens(screen_id),
    question        TEXT NOT NULL,
    options_json    TEXT,
    answer          TEXT,
    correctness     TEXT CHECK (correctness IS NULL OR correctness IN ('correct','wrong','unsubmitted')),  -- F1
    kb_chunks_ref   TEXT,
    captured_at     INTEGER NOT NULL DEFAULT (CAST(unixepoch('subsec')*1000 AS INTEGER))
);
CREATE INDEX IF NOT EXISTS idx_qa_screen ON qa_captures(screen_id);

-- ---------------------------------------------------------------------------
-- CONTEXT SLICES — L0-L3 JIT index. CURRENCY is a constraint, not a
-- convention (F11): at most ONE non-superseded row per (platform,level,
-- selector) — NULL platform normalized so L0 cannot duplicate. Supersession
-- is explicit Type-2 SCD.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS context_slices (
    slice_id        TEXT PRIMARY KEY,
    platform        TEXT,
    level           INTEGER NOT NULL CHECK (level IN (0,1,2,3)),
    selector        TEXT NOT NULL,
    source_path     TEXT NOT NULL,
    source_sha      TEXT NOT NULL,
    trust           TEXT NOT NULL DEFAULT 'trusted'
                    CHECK (trust IN ('provisional','trusted','superseded')),
    verified_count  INTEGER NOT NULL DEFAULT 0,
    superseded_at   INTEGER,                                       -- F11 SCD
    superseded_by   TEXT REFERENCES context_slices(slice_id),      -- F11 SCD
    ingested_at     INTEGER NOT NULL DEFAULT (CAST(unixepoch('subsec')*1000 AS INTEGER))
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_slice_current
  ON context_slices(ifnull(platform,'*'), level, selector) WHERE trust <> 'superseded';  -- F11
CREATE INDEX IF NOT EXISTS idx_slices_lookup ON context_slices(platform, level, selector, trust);

-- Content-addressed slice bodies (F13): R10.6 mandates in-place YAML edits, so
-- a served sha would otherwise name bytes that no longer exist — making
-- receipts unverifiable. Files stay canonical (R10.3); slice_blobs is
-- rebuildable-going-forward, immutable-backward. R9.7 becomes real.
CREATE TABLE IF NOT EXISTS slice_blobs (
    sha             TEXT PRIMARY KEY,
    body            TEXT NOT NULL,
    bytes           INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS bundle_receipts (
    bundle_id       TEXT PRIMARY KEY,
    call_kind       TEXT NOT NULL CHECK (call_kind IN ('classify','bt_build','retry_build','diagnose','extract')),
    screen_id       TEXT,
    slices_json     TEXT NOT NULL,
    dropped_json    TEXT NOT NULL DEFAULT '[]',
    kb_chunks_json  TEXT,
    total_chars     INTEGER NOT NULL,
    receipt_sha     TEXT NOT NULL,                                 -- F13: NOT NULL, resolvable via slice_blobs
    created_at      INTEGER NOT NULL DEFAULT (CAST(unixepoch('subsec')*1000 AS INTEGER))
);

-- ---------------------------------------------------------------------------
-- COORDINATION — escalation state machine. platform column for scoped RCA
-- (F19). Dedup structures are TABLES with PK-conflict semantics (F17), never
-- JSON columns (a JSON dedup column is the /tmp flag file reintroduced).
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS coordination (
    screen_id       TEXT PRIMARY KEY REFERENCES screens(screen_id),
    platform        TEXT REFERENCES platforms(platform),           -- F19
    state           TEXT NOT NULL DEFAULT 'normal'
                    CHECK (state IN ('normal','consulting','diagnosing','awaiting_resume','terminal','cleared')),
    tier            TEXT CHECK (tier IS NULL OR tier IN ('tier1','tier2','tier3','terminal')),  -- F1
    attempt_count   INTEGER NOT NULL DEFAULT 0,
    last_attempt_key TEXT,
    terminal        INTEGER NOT NULL DEFAULT 0,
    resume_at       INTEGER,                                       -- ms epoch (F14)
    response_pending_until INTEGER,                                -- ms epoch (O9)
    yaml_sha_at_attempt TEXT,
    user_instructions TEXT,
    cleared_reason  TEXT CHECK (cleared_reason IS NULL OR cleared_reason IN
                    ('user_stop_abandon','user_stop_reset','advanced','yaml_fold')),            -- F1
    updated_at      INTEGER NOT NULL DEFAULT (CAST(unixepoch('subsec')*1000 AS INTEGER))
);
CREATE INDEX IF NOT EXISTS idx_coordination_active
  ON coordination(platform, state, tier) WHERE state <> 'normal';  -- F19

-- Exactly-once dispatch/notify: INSERT with PK conflict = already done (F17).
CREATE TABLE IF NOT EXISTS tier_dispatches (
    screen_id       TEXT NOT NULL REFERENCES screens(screen_id),
    tier            TEXT NOT NULL CHECK (tier IN ('tier1','tier2','tier3')),
    cycle_id        TEXT NOT NULL,
    dispatched_at   INTEGER NOT NULL DEFAULT (CAST(unixepoch('subsec')*1000 AS INTEGER)),
    PRIMARY KEY (screen_id, tier, cycle_id)
);
CREATE TABLE IF NOT EXISTS notify_cycles (
    screen_id       TEXT NOT NULL REFERENCES screens(screen_id),
    cycle_id        TEXT NOT NULL,
    notified_at     INTEGER NOT NULL DEFAULT (CAST(unixepoch('subsec')*1000 AS INTEGER)),
    PRIMARY KEY (screen_id, cycle_id)
);

-- Ladder invariants (R8.3/R8.5), closed against every demonstrated attack:
CREATE TRIGGER IF NOT EXISTS coordination_monotonic
BEFORE UPDATE ON coordination
WHEN NEW.attempt_count < OLD.attempt_count AND NEW.state <> 'cleared'
BEGIN SELECT RAISE(ABORT, 'attempt_count is monotonic (R8.3)'); END;

CREATE TRIGGER IF NOT EXISTS coordination_sticky_terminal
BEFORE UPDATE ON coordination
WHEN OLD.terminal = 1 AND NEW.terminal = 0 AND NEW.state <> 'cleared'
BEGIN SELECT RAISE(ABORT, 'terminal is sticky (R8.3)'); END;

CREATE TRIGGER IF NOT EXISTS coordination_clear_requires_reason
BEFORE UPDATE ON coordination
WHEN NEW.state = 'cleared' AND OLD.state <> 'cleared' AND NEW.cleared_reason IS NULL
BEGIN SELECT RAISE(ABORT, 'clearing requires an authorized cleared_reason (R8.5)'); END;

CREATE TRIGGER IF NOT EXISTS coordination_cleared_is_immutable                    -- F2
BEFORE UPDATE ON coordination
WHEN OLD.state = 'cleared'
 AND (NEW.attempt_count < OLD.attempt_count OR (OLD.terminal = 1 AND NEW.terminal = 0))
BEGIN SELECT RAISE(ABORT, 'a cleared row is not a mutable row (R8.3)'); END;

CREATE TRIGGER IF NOT EXISTS coordination_no_delete                               -- F3
BEFORE DELETE ON coordination
BEGIN SELECT RAISE(ABORT, 'coordination rows are never deleted (R8.3)'); END;

CREATE TRIGGER IF NOT EXISTS coordination_no_reinsert                             -- F3
BEFORE INSERT ON coordination
WHEN EXISTS (SELECT 1 FROM coordination WHERE screen_id = NEW.screen_id)
BEGIN SELECT RAISE(ABORT, 'no INSERT OR REPLACE over a live ladder (R8.3)'); END;

CREATE TRIGGER IF NOT EXISTS coordination_fold_cannot_unterminate                 -- F4 (guards NEW.terminal:
BEFORE UPDATE ON coordination                                                     --  the fold-CLEAR of a terminal
WHEN NEW.state = 'cleared' AND OLD.terminal = 1 AND NEW.terminal = 0              --  screen's ladder is LEGAL)
 AND NEW.cleared_reason = 'yaml_fold'
BEGIN SELECT RAISE(ABORT, 'a YAML fold clears the ladder but never un-terminates (R8.5)'); END;

CREATE TRIGGER IF NOT EXISTS coordination_tier_monotonic                          -- F6
BEFORE UPDATE ON coordination
WHEN OLD.tier IS NOT NULL AND NEW.tier IS NOT NULL AND NEW.state <> 'cleared'
 AND (CASE NEW.tier WHEN 'tier1' THEN 1 WHEN 'tier2' THEN 2 WHEN 'tier3' THEN 3 WHEN 'terminal' THEN 4 END)
   < (CASE OLD.tier WHEN 'tier1' THEN 1 WHEN 'tier2' THEN 2 WHEN 'tier3' THEN 3 WHEN 'terminal' THEN 4 END)
BEGIN SELECT RAISE(ABORT, 'tier is monotonic (R8.2/R8.3)'); END;

CREATE TRIGGER IF NOT EXISTS coordination_attempt_ceiling                         -- F6
BEFORE UPDATE ON coordination
WHEN NEW.attempt_count > 4
BEGIN SELECT RAISE(ABORT, 'the ladder is 4 attempts (R8.2); >4 escaped the funnel'); END;

CREATE TRIGGER IF NOT EXISTS coordination_terminal_implies_state                  -- F6
BEFORE UPDATE ON coordination
WHEN NEW.terminal = 1 AND NEW.state NOT IN ('terminal','cleared')
BEGIN SELECT RAISE(ABORT, 'terminal=1 implies state IN (terminal,cleared)'); END;

-- ---------------------------------------------------------------------------
-- CONSULTS — R8.6 "ONE consultation at a time, globally" is a CONSTRAINT (the
-- most expensive invariant in the system costs one line — GAIA).
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS consults (
    consult_id      TEXT PRIMARY KEY,
    screen_id       TEXT REFERENCES screens(screen_id),
    platform        TEXT NOT NULL,
    status          TEXT NOT NULL DEFAULT 'pending'
                    CHECK (status IN ('pending','complete','worker_failed','abandoned')),
    abandon_reason  TEXT,
    payload_dir     TEXT NOT NULL,
    failure_reason  TEXT,
    created_at      INTEGER NOT NULL DEFAULT (CAST(unixepoch('subsec')*1000 AS INTEGER)),
    resolved_at     INTEGER
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_one_pending_consult
  ON consults(status) WHERE status = 'pending';                    -- R8.6

-- ---------------------------------------------------------------------------
-- EVENTS — append-only, ordered by event_id NEVER created_at (F14). Retention
-- = archive-before-delete with FAITHFUL-COPY verification + id retirement (F5).
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS events (
    event_id        INTEGER PRIMARY KEY AUTOINCREMENT,
    kind            TEXT NOT NULL,
    platform        TEXT,
    screen_id       TEXT,
    consult_id      TEXT,
    actor           TEXT NOT NULL CHECK (actor IN ('api','worker','mac','operator','supervisor','system')),
    payload_json    TEXT NOT NULL DEFAULT '{}',
    created_at      INTEGER NOT NULL DEFAULT (CAST(unixepoch('subsec')*1000 AS INTEGER))
);
CREATE INDEX IF NOT EXISTS idx_events_screen ON events(screen_id, kind, event_id DESC);
CREATE INDEX IF NOT EXISTS idx_events_platform ON events(platform, kind, event_id DESC);

CREATE TABLE IF NOT EXISTS events_archive (
    event_id        INTEGER PRIMARY KEY,
    kind            TEXT NOT NULL,
    platform        TEXT,
    screen_id       TEXT,
    consult_id      TEXT,
    actor           TEXT NOT NULL,
    payload_json    TEXT NOT NULL,
    created_at      INTEGER NOT NULL,
    archived_at     INTEGER NOT NULL DEFAULT (CAST(unixepoch('subsec')*1000 AS INTEGER))
);

CREATE TRIGGER IF NOT EXISTS events_append_only
BEFORE UPDATE ON events
BEGIN SELECT RAISE(ABORT, 'events are append-only'); END;

CREATE TRIGGER IF NOT EXISTS events_archive_must_match                            -- F5c
BEFORE DELETE ON events
WHEN NOT EXISTS (
  SELECT 1 FROM events_archive a WHERE a.event_id = OLD.event_id
    AND a.kind = OLD.kind AND a.actor = OLD.actor
    AND a.payload_json = OLD.payload_json AND a.created_at = OLD.created_at)
BEGIN SELECT RAISE(ABORT, 'archive-before-delete: the archive row must be a faithful copy'); END;

CREATE TRIGGER IF NOT EXISTS events_archive_append_only                           -- F5a
BEFORE UPDATE ON events_archive
BEGIN SELECT RAISE(ABORT, 'events_archive is append-only'); END;

CREATE TRIGGER IF NOT EXISTS events_archive_no_delete                             -- F5a
BEFORE DELETE ON events_archive
BEGIN SELECT RAISE(ABORT, 'events_archive is the floor: nothing is deleted from it'); END;

CREATE TRIGGER IF NOT EXISTS events_no_id_reuse                                   -- F5b
BEFORE INSERT ON events
WHEN EXISTS (SELECT 1 FROM events_archive WHERE event_id = NEW.event_id)
BEGIN SELECT RAISE(ABORT, 'event_id is retired once archived; never reused'); END;
