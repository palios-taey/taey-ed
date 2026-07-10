-- ============================================================================
-- taey-ed STATE STORE — formal DDL v3 (taey_state.db, separate from taey_ed.db)
-- Design: docs/STATE_STORE_DESIGN.md · Contract: docs/REQUIREMENTS.md
-- v3 (2026-07-10): closes the CLASSES HORIZON proved open in v2 (v2 blocked the
-- exact attack strings, not the equivalence families). The enforcement model is
-- now TWO layers, per Horizon: (1) this DDL closes STRUCTURE/TYPE/SHAPE by
-- construction; (2) AUTHORIZATION + evidence + "who did this" live in the app
-- repository chokepoint (spark/state_repo.py, p2) — triggers cannot verify a
-- caller's role, so they don't pretend to.
--
-- STRUCTURAL closures in this file:
--  * STRICT tables everywhere → declared types are ENFORCED (kills text/second-
--    scale sneaking into INTEGER ms columns; magnitude CHECK adds the ms floor).
--  * WITHOUT ROWID + NOT NULL on every logical PK → kills NULL-primary-key
--    duplicate identities (SQLite rowid PKs otherwise admit NULL).
--  * INSERT OR REPLACE is neutralized: recursive_triggers=ON (asserted in init,
--    see below) makes REPLACE's implicit DELETE fire the BEFORE DELETE guards;
--    PLUS a BEFORE INSERT no-clobber guard on every append-only/identity table
--    so a same-PK re-insert aborts regardless of the recursive_triggers setting.
--  * coordination: leaving 'cleared', reassigning screen_id, and INSERT-path
--    tier/attempt/terminal violations are all closed (v2 guarded only UPDATE).
--  * events_archive faithful-copy compares ALL lineage columns NULL-safe (IS).
--
-- REQUIRED init PRAGMAs (asserted for EVERY writer connection; not optional):
--   PRAGMA journal_mode=WAL; PRAGMA busy_timeout=5000; PRAGMA synchronous=NORMAL;
--   PRAGMA foreign_keys=ON; PRAGMA recursive_triggers=ON;
-- Timestamps: INTEGER ms since epoch; ONE app helper; STRICT + magnitude CHECK.
-- ============================================================================

CREATE TABLE IF NOT EXISTS platforms (
    platform        TEXT NOT NULL PRIMARY KEY,
    display_name    TEXT,
    onboarded_at    INTEGER NOT NULL DEFAULT (CAST(unixepoch('subsec')*1000 AS INTEGER)) CHECK (onboarded_at > 1000000000000),
    knowledge_path  TEXT,
    knowledge_sha   TEXT,
    status          TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('researching','active','paused'))
) STRICT, WITHOUT ROWID;

-- Receipts declared EARLY so screens/behavior_trees FKs resolve. A receipt is
-- an immutable record of what an LLM call was served (F7/F13, R7.5/R9.7).
CREATE TABLE IF NOT EXISTS bundle_receipts (
    bundle_id       TEXT NOT NULL PRIMARY KEY,
    call_kind       TEXT NOT NULL CHECK (call_kind IN ('classify','bt_build','retry_build','diagnose','extract')),
    screen_id       TEXT,
    slices_json     TEXT NOT NULL,
    dropped_json    TEXT NOT NULL DEFAULT '[]',
    kb_chunks_json  TEXT,
    total_chars     INTEGER NOT NULL,
    receipt_sha     TEXT NOT NULL,
    created_at      INTEGER NOT NULL DEFAULT (CAST(unixepoch('subsec')*1000 AS INTEGER)) CHECK (created_at > 1000000000000)
) STRICT, WITHOUT ROWID;
CREATE TRIGGER IF NOT EXISTS bundle_receipts_immutable
BEFORE UPDATE ON bundle_receipts
BEGIN SELECT RAISE(ABORT,'bundle receipts are immutable (R9.7)'); END;
CREATE TRIGGER IF NOT EXISTS bundle_receipts_no_delete
BEFORE DELETE ON bundle_receipts
BEGIN SELECT RAISE(ABORT,'bundle receipts are never deleted (audit floor)'); END;

-- Content-addressed slice bodies (F13): receipts stay verifiable after in-place
-- YAML edits. Immutable by content address.
CREATE TABLE IF NOT EXISTS slice_blobs (
    sha             TEXT NOT NULL PRIMARY KEY,
    body            TEXT NOT NULL,
    bytes           INTEGER NOT NULL CHECK (bytes >= 0)
) STRICT, WITHOUT ROWID;
CREATE TRIGGER IF NOT EXISTS slice_blobs_immutable
BEFORE UPDATE ON slice_blobs
BEGIN SELECT RAISE(ABORT,'slice_blobs are content-addressed and immutable'); END;
-- Horizon r4 C: slice_blobs had only a BEFORE UPDATE guard — a same-sha
-- INSERT OR REPLACE deleted+reinserted a forged body. Content-addressed =
-- fully immutable: no delete, no re-insert.
CREATE TRIGGER IF NOT EXISTS slice_blobs_no_delete
BEFORE DELETE ON slice_blobs
BEGIN SELECT RAISE(ABORT,'slice_blobs are immutable: never deleted/REPLACEd'); END;
CREATE TRIGGER IF NOT EXISTS slice_blobs_no_clobber
BEFORE INSERT ON slice_blobs
WHEN EXISTS (SELECT 1 FROM slice_blobs WHERE sha = NEW.sha)
BEGIN SELECT RAISE(ABORT,'slice_blob sha already stored; content-addressed bodies never change'); END;

-- ---------------------------------------------------------------------------
-- SCREEN TYPE REGISTRY — durable TYPE mapping (map once, never redo)
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS screen_types (
    platform        TEXT NOT NULL REFERENCES platforms(platform),
    screen_type     TEXT NOT NULL,
    category        TEXT NOT NULL CHECK (category IN ('NAVIGATION','VIDEO','ARTICLE','EXERCISE','TRANSITION')),
    artifact_path   TEXT NOT NULL,
    artifact_sha    TEXT NOT NULL,
    deterministic   INTEGER NOT NULL DEFAULT 0 CHECK (deterministic IN (0,1)),
    trust           TEXT NOT NULL DEFAULT 'provisional' CHECK (trust IN ('provisional','trusted','demoted')),
    validated_successes INTEGER NOT NULL DEFAULT 0 CHECK (validated_successes >= 0),
    ingested_at     INTEGER NOT NULL DEFAULT (CAST(unixepoch('subsec')*1000 AS INTEGER)) CHECK (ingested_at > 1000000000000),
    PRIMARY KEY (platform, screen_type),
    CHECK (screen_type <> category),
    CHECK (category <> 'EXERCISE' OR deterministic = 0),
    CHECK (trust <> 'trusted' OR validated_successes >= 3)
) STRICT, WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS type_signatures (
    platform        TEXT NOT NULL,
    screen_type     TEXT NOT NULL,
    sig_kind        TEXT NOT NULL CHECK (sig_kind IN ('widget_set','marker_set','dom_class')),
    sig_value       TEXT NOT NULL,
    created_at      INTEGER NOT NULL DEFAULT (CAST(unixepoch('subsec')*1000 AS INTEGER)) CHECK (created_at > 1000000000000),
    PRIMARY KEY (platform, sig_kind, sig_value, screen_type),
    FOREIGN KEY (platform, screen_type) REFERENCES screen_types(platform, screen_type)
) STRICT, WITHOUT ROWID;

-- ---------------------------------------------------------------------------
-- SCREENS — INSTANCE identity. Classification unforgeable: 'classified' needs
-- a real type AND a classify receipt for THIS screen (F7).
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS screens (
    screen_id       TEXT NOT NULL PRIMARY KEY,
    platform        TEXT NOT NULL REFERENCES platforms(platform),
    screen_type     TEXT,
    classification  TEXT NOT NULL DEFAULT 'pending'
                    CHECK (classification IN ('pending','classified','failed_retryable','operator_required')),
    classified_by_bundle_id TEXT REFERENCES bundle_receipts(bundle_id),
    question_fingerprint TEXT,
    first_seen      INTEGER NOT NULL DEFAULT (CAST(unixepoch('subsec')*1000 AS INTEGER)) CHECK (first_seen > 1000000000000),
    last_seen       INTEGER NOT NULL DEFAULT (CAST(unixepoch('subsec')*1000 AS INTEGER)) CHECK (last_seen > 1000000000000),
    retired         INTEGER NOT NULL DEFAULT 0 CHECK (retired IN (0,1)),
    FOREIGN KEY (platform, screen_type) REFERENCES screen_types(platform, screen_type),
    CHECK (classification <> 'classified'
           OR (screen_type IS NOT NULL AND classified_by_bundle_id IS NOT NULL))
) STRICT, WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS idx_screens_platform_type ON screens(platform, screen_type);
-- F7 close: the linked receipt must actually be a classify receipt (an
-- unrelated synthetic receipt no longer launders a classification).
CREATE TRIGGER IF NOT EXISTS screens_classify_receipt_valid_ins
BEFORE INSERT ON screens
WHEN NEW.classification = 'classified' AND (
     NEW.classified_by_bundle_id IS NULL
  OR NOT EXISTS (SELECT 1 FROM bundle_receipts b
                 WHERE b.bundle_id = NEW.classified_by_bundle_id
                   AND b.call_kind = 'classify'
                   AND b.screen_id = NEW.screen_id))
BEGIN SELECT RAISE(ABORT,'classified requires a classify receipt for THIS screen (F7)'); END;
CREATE TRIGGER IF NOT EXISTS screens_classify_receipt_valid_upd
BEFORE UPDATE ON screens
WHEN NEW.classification = 'classified' AND (
     NEW.classified_by_bundle_id IS NULL
  OR NOT EXISTS (SELECT 1 FROM bundle_receipts b
                 WHERE b.bundle_id = NEW.classified_by_bundle_id
                   AND b.call_kind = 'classify'
                   AND b.screen_id = NEW.screen_id))
BEGIN SELECT RAISE(ABORT,'classified requires a classify receipt for THIS screen (F7)'); END;

CREATE TABLE IF NOT EXISTS screen_keys (
    platform        TEXT NOT NULL,
    key_kind        TEXT NOT NULL CHECK (key_kind IN ('skeleton','signature','widget_set')),
    key_hash        TEXT NOT NULL,
    screen_id       TEXT NOT NULL REFERENCES screens(screen_id),
    created_at      INTEGER NOT NULL DEFAULT (CAST(unixepoch('subsec')*1000 AS INTEGER)) CHECK (created_at > 1000000000000),
    PRIMARY KEY (platform, key_kind, key_hash, screen_id)
) STRICT, WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS idx_screen_keys_lookup ON screen_keys(platform, key_kind, key_hash);

CREATE TABLE IF NOT EXISTS screen_features (
    screen_id       TEXT NOT NULL REFERENCES screens(screen_id),
    feature_kind    TEXT NOT NULL,
    feature_value   TEXT NOT NULL,
    PRIMARY KEY (screen_id, feature_kind)
) STRICT, WITHOUT ROWID;

-- ---------------------------------------------------------------------------
-- BEHAVIOR TREES — versioned; exactly ONE validated per screen; unforgeable.
-- REPLACE that would destroy history is blocked by the no-clobber INSERT guard.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS behavior_trees (
    bt_id           TEXT NOT NULL PRIMARY KEY,
    screen_id       TEXT NOT NULL REFERENCES screens(screen_id),
    revision        INTEGER NOT NULL,
    bt_json         TEXT NOT NULL,
    built_by        TEXT NOT NULL,
    source_kind     TEXT NOT NULL,
    type_artifact_sha TEXT,
    bundle_id       TEXT REFERENCES bundle_receipts(bundle_id),
    status          TEXT NOT NULL DEFAULT 'candidate'
                    CHECK (status IN ('candidate','validated','demoted','retired','rejected')),
    success_count   INTEGER NOT NULL DEFAULT 0 CHECK (success_count >= 0),
    consecutive_failures INTEGER NOT NULL DEFAULT 0 CHECK (consecutive_failures >= 0),
    supersedes      TEXT REFERENCES behavior_trees(bt_id),
    created_at      INTEGER NOT NULL DEFAULT (CAST(unixepoch('subsec')*1000 AS INTEGER)) CHECK (created_at > 1000000000000),
    UNIQUE (screen_id, revision),
    CHECK (status <> 'validated' OR success_count >= 1),
    CHECK (status <> 'validated' OR consecutive_failures < 2)
) STRICT, WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS idx_bt_current ON behavior_trees(screen_id, status);
CREATE UNIQUE INDEX IF NOT EXISTS ux_bt_one_validated ON behavior_trees(screen_id) WHERE status = 'validated';
CREATE TRIGGER IF NOT EXISTS behavior_trees_no_clobber
BEFORE INSERT ON behavior_trees
WHEN EXISTS (SELECT 1 FROM behavior_trees WHERE bt_id = NEW.bt_id)
BEGIN SELECT RAISE(ABORT,'no REPLACE over a behavior tree (history is immutable)'); END;
-- v3.1 (Horizon r3 D): REPLACE resolves conflicts on ANY unique target, not
-- just the PK. behavior_trees has UNIQUE(screen_id,revision) + the validated
-- partial index — so a REPLACE with a NEW bt_id but a colliding (screen_id,
-- revision) would implicit-DELETE the original and slip past the bt_id guard.
-- General close: block ALL deletes (BT history is immutable; supersede via
-- status, never delete) — with recursive_triggers=ON this aborts every
-- REPLACE's implicit delete regardless of which unique it targets.
CREATE TRIGGER IF NOT EXISTS behavior_trees_no_delete
BEFORE DELETE ON behavior_trees
BEGIN SELECT RAISE(ABORT,'behavior tree history is immutable — supersede via status, never delete/REPLACE'); END;
-- identity/content/provenance columns are immutable (only status + counters mutate)
-- Horizon r4 B: the UPSERT (ON CONFLICT DO UPDATE) path is an UPDATE, not a
-- REPLACE-delete — so it must be caught by naming EVERY immutable column
-- (type_artifact_sha + created_at were omitted in v3.1). Only status +
-- success_count + consecutive_failures may ever mutate.
CREATE TRIGGER IF NOT EXISTS behavior_trees_identity_immutable
BEFORE UPDATE OF bt_id, screen_id, revision, bt_json, built_by, source_kind, bundle_id, supersedes, type_artifact_sha, created_at ON behavior_trees
BEGIN SELECT RAISE(ABORT,'behavior tree identity/content/provenance is immutable (only status + counters mutate)'); END;

CREATE TABLE IF NOT EXISTS qa_captures (
    qa_id           TEXT NOT NULL PRIMARY KEY,
    screen_id       TEXT NOT NULL REFERENCES screens(screen_id),
    question        TEXT NOT NULL,
    options_json    TEXT,
    answer          TEXT,
    correctness     TEXT CHECK (correctness IS NULL OR correctness IN ('correct','wrong','unsubmitted')),
    kb_chunks_ref   TEXT,
    captured_at     INTEGER NOT NULL DEFAULT (CAST(unixepoch('subsec')*1000 AS INTEGER)) CHECK (captured_at > 1000000000000)
) STRICT, WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS idx_qa_screen ON qa_captures(screen_id);

-- ---------------------------------------------------------------------------
-- CONTEXT SLICES — L0-L3 JIT index; SCD currency as a constraint.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS context_slices (
    slice_id        TEXT NOT NULL PRIMARY KEY,
    platform        TEXT,
    level           INTEGER NOT NULL CHECK (level IN (0,1,2,3)),
    selector        TEXT NOT NULL,
    source_path     TEXT NOT NULL,
    source_sha      TEXT NOT NULL,
    trust           TEXT NOT NULL DEFAULT 'trusted' CHECK (trust IN ('provisional','trusted','superseded')),
    verified_count  INTEGER NOT NULL DEFAULT 0 CHECK (verified_count >= 0),
    superseded_at   INTEGER CHECK (superseded_at IS NULL OR superseded_at > 1000000000000),
    superseded_by   TEXT REFERENCES context_slices(slice_id),
    ingested_at     INTEGER NOT NULL DEFAULT (CAST(unixepoch('subsec')*1000 AS INTEGER)) CHECK (ingested_at > 1000000000000)
) STRICT, WITHOUT ROWID;
CREATE UNIQUE INDEX IF NOT EXISTS ux_slice_current
  ON context_slices(ifnull(platform,'*'), level, selector) WHERE trust <> 'superseded';
CREATE INDEX IF NOT EXISTS idx_slices_lookup ON context_slices(platform, level, selector, trust);
-- v3.1 (Horizon r3 D): supersession is an UPDATE (trust->'superseded'); a slice
-- is never deleted. Blocks REPLACE via ux_slice_current from silently deleting
-- the current row.
CREATE TRIGGER IF NOT EXISTS context_slices_no_delete
BEFORE DELETE ON context_slices
BEGIN SELECT RAISE(ABORT,'context slices supersede (SCD), never delete/REPLACE'); END;
-- Horizon r4 A: an UPSERT (ON CONFLICT DO UPDATE) on ux_slice_current rewrote a
-- current slice's source_path/source_sha via the UPDATE path (no_delete never
-- fires). A slice's IDENTITY + SOURCE is immutable; only the SCD lifecycle
-- (trust->superseded + superseded_at/by + verified_count) mutates.
CREATE TRIGGER IF NOT EXISTS context_slices_source_immutable
BEFORE UPDATE OF slice_id, platform, level, selector, source_path, source_sha, ingested_at ON context_slices
BEGIN SELECT RAISE(ABORT,'context slice identity/source is immutable; supersede via a new row (SCD)'); END;

-- ---------------------------------------------------------------------------
-- COORDINATION — escalation state machine. v3 closes: leaving 'cleared',
-- screen_id reassignment, and the INSERT path (v2 guarded only UPDATE).
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS coordination (
    screen_id       TEXT NOT NULL PRIMARY KEY REFERENCES screens(screen_id),
    platform        TEXT REFERENCES platforms(platform),
    state           TEXT NOT NULL DEFAULT 'normal'
                    CHECK (state IN ('normal','consulting','diagnosing','awaiting_resume','terminal','cleared')),
    tier            TEXT CHECK (tier IS NULL OR tier IN ('tier1','tier2','tier3','terminal')),
    attempt_count   INTEGER NOT NULL DEFAULT 0 CHECK (attempt_count >= 0 AND attempt_count <= 4),  -- ceiling on INSERT too (F6)
    last_attempt_key TEXT,
    terminal        INTEGER NOT NULL DEFAULT 0 CHECK (terminal IN (0,1)),
    resume_at       INTEGER CHECK (resume_at IS NULL OR resume_at > 1000000000000),
    response_pending_until INTEGER CHECK (response_pending_until IS NULL OR response_pending_until > 1000000000000),
    yaml_sha_at_attempt TEXT,
    user_instructions TEXT,
    cleared_reason  TEXT CHECK (cleared_reason IS NULL OR cleared_reason IN
                    ('user_stop_abandon','user_stop_reset','advanced','yaml_fold')),
    updated_at      INTEGER NOT NULL DEFAULT (CAST(unixepoch('subsec')*1000 AS INTEGER)) CHECK (updated_at > 1000000000000),
    CHECK (terminal = 0 OR state IN ('terminal','cleared')),          -- F6 on INSERT
    CHECK (state <> 'cleared' OR cleared_reason IS NOT NULL)          -- reason on INSERT too
) STRICT, WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS idx_coordination_active
  ON coordination(platform, state, tier) WHERE state <> 'normal';

CREATE TRIGGER IF NOT EXISTS coordination_identity_immutable
BEFORE UPDATE OF screen_id ON coordination
BEGIN SELECT RAISE(ABORT,'screen_id is identity; a ladder cannot be reassigned (R8.3)'); END;
CREATE TRIGGER IF NOT EXISTS coordination_no_delete
BEFORE DELETE ON coordination
BEGIN SELECT RAISE(ABORT,'coordination rows are never deleted (R8.3)'); END;
CREATE TRIGGER IF NOT EXISTS coordination_no_clobber
BEFORE INSERT ON coordination
WHEN EXISTS (SELECT 1 FROM coordination WHERE screen_id = NEW.screen_id)
BEGIN SELECT RAISE(ABORT,'no REPLACE over a live ladder (R8.3)'); END;
-- The authorized RE-ARM (cleared -> fresh normal ladder) is the ONE legal exit
-- from cleared. Define it once; monotonic + sticky exempt it so a cleared row
-- with attempt>0 / terminal=1 CAN re-arm (Horizon r3 B: the re-arm was
-- over-blocked). Reused via the identical predicate in each WHEN.
CREATE TRIGGER IF NOT EXISTS coordination_monotonic
BEFORE UPDATE ON coordination
WHEN NEW.attempt_count < OLD.attempt_count AND NEW.state <> 'cleared'
 AND NOT (OLD.state = 'cleared' AND NEW.state = 'normal' AND NEW.attempt_count = 0 AND NEW.terminal = 0 AND NEW.tier IS NULL)
BEGIN SELECT RAISE(ABORT,'attempt_count is monotonic (R8.3)'); END;
CREATE TRIGGER IF NOT EXISTS coordination_sticky_terminal
BEFORE UPDATE ON coordination
WHEN OLD.terminal = 1 AND NEW.terminal = 0 AND NEW.state <> 'cleared'
 AND NOT (OLD.state = 'cleared' AND NEW.state = 'normal' AND NEW.attempt_count = 0 AND NEW.terminal = 0 AND NEW.tier IS NULL)
BEGIN SELECT RAISE(ABORT,'terminal is sticky (R8.3)'); END;
-- 'cleared' is FROZEN: the ONLY legal UPDATE of a cleared row is the authorized
-- re-arm to a fresh normal ladder. Any other change — leaving cleared to a
-- non-normal state, OR mutating ANY field while staying cleared (platform,
-- attempt_count, tier, terminal, last_attempt_key) — aborts. (Horizon r3 B:
-- v3 only caught cleared->consulting + decrement; a cleared row could still
-- mutate other fields while staying cleared.)
-- Horizon r4 D: the frozen no-op must compare EVERY column (v3.1 omitted
-- resume_at, response_pending_until, yaml_sha_at_attempt, user_instructions,
-- updated_at), and the re-arm must be exactly the core-field reset and NOTHING
-- else — the four-field shape must not exempt simultaneous mutation of the
-- others. Re-arm = the core reset AND every non-core field cleared to a
-- well-defined fresh value.
CREATE TRIGGER IF NOT EXISTS coordination_cleared_frozen
BEFORE UPDATE ON coordination
WHEN OLD.state = 'cleared'
 AND NOT (  -- the authorized re-arm: reset ONLY the core ladder; every non-core
            -- field must be UNCHANGED (so the re-arm shape cannot smuggle a
            -- forged deadline / instruction / provenance — Horizon r4 D)
      NEW.state = 'normal' AND NEW.attempt_count = 0 AND NEW.terminal = 0 AND NEW.tier IS NULL
      AND NEW.platform IS OLD.platform
      AND NEW.last_attempt_key IS OLD.last_attempt_key AND NEW.resume_at IS OLD.resume_at
      AND NEW.response_pending_until IS OLD.response_pending_until
      AND NEW.yaml_sha_at_attempt IS OLD.yaml_sha_at_attempt
      AND NEW.user_instructions IS OLD.user_instructions
      AND NEW.cleared_reason IS OLD.cleared_reason)
 AND NOT (  -- or a frozen no-op: EVERY column unchanged (updated_at may bump)
      NEW.state = 'cleared'
      AND NEW.attempt_count = OLD.attempt_count AND NEW.terminal = OLD.terminal
      AND NEW.tier IS OLD.tier AND NEW.platform IS OLD.platform
      AND NEW.last_attempt_key IS OLD.last_attempt_key AND NEW.cleared_reason IS OLD.cleared_reason
      AND NEW.resume_at IS OLD.resume_at AND NEW.response_pending_until IS OLD.response_pending_until
      AND NEW.yaml_sha_at_attempt IS OLD.yaml_sha_at_attempt AND NEW.user_instructions IS OLD.user_instructions)
BEGIN SELECT RAISE(ABORT,'a cleared ladder is frozen: only an authorized full re-arm to a fresh normal ladder is legal (R8.3/R8.5)'); END;
CREATE TRIGGER IF NOT EXISTS coordination_clear_requires_reason
BEFORE UPDATE ON coordination
WHEN NEW.state = 'cleared' AND OLD.state <> 'cleared' AND NEW.cleared_reason IS NULL
BEGIN SELECT RAISE(ABORT,'clearing requires an authorized cleared_reason (R8.5)'); END;
CREATE TRIGGER IF NOT EXISTS coordination_fold_cannot_unterminate
BEFORE UPDATE ON coordination
WHEN NEW.state = 'cleared' AND OLD.terminal = 1 AND NEW.terminal = 0 AND NEW.cleared_reason = 'yaml_fold'
BEGIN SELECT RAISE(ABORT,'a YAML fold clears the ladder but never un-terminates (R8.5)'); END;
-- Horizon r4 E: the NULL bridge — set tier=NULL then tier=tier1 — downgraded
-- the ladder because the guard required BOTH tiers non-NULL. Fix: once a tier
-- is set, it may only advance; going to NULL or a lower rank is blocked EXCEPT
-- through the authorized cleared->normal re-arm.
CREATE TRIGGER IF NOT EXISTS coordination_tier_monotonic
BEFORE UPDATE ON coordination
WHEN OLD.tier IS NOT NULL AND NEW.state <> 'cleared'
 AND NOT (OLD.state = 'cleared' AND NEW.state = 'normal' AND NEW.tier IS NULL AND NEW.attempt_count = 0 AND NEW.terminal = 0)  -- re-arm
 AND (NEW.tier IS NULL
      OR (CASE NEW.tier WHEN 'tier1' THEN 1 WHEN 'tier2' THEN 2 WHEN 'tier3' THEN 3 WHEN 'terminal' THEN 4 END)
       < (CASE OLD.tier WHEN 'tier1' THEN 1 WHEN 'tier2' THEN 2 WHEN 'tier3' THEN 3 WHEN 'terminal' THEN 4 END))
BEGIN SELECT RAISE(ABORT,'tier is monotonic — no downgrade, no NULL bridge (R8.2/R8.3)'); END;
CREATE TRIGGER IF NOT EXISTS coordination_terminal_implies_state
BEFORE UPDATE ON coordination
WHEN NEW.terminal = 1 AND NEW.state NOT IN ('terminal','cleared')
BEGIN SELECT RAISE(ABORT,'terminal=1 implies state IN (terminal,cleared)'); END;

-- Exactly-once dispatch/notify: PK-conflict tables. No-clobber guards defeat
-- INSERT OR REPLACE / delete+reinsert (Horizon: REPLACE defeated the plain PK).
CREATE TABLE IF NOT EXISTS tier_dispatches (
    screen_id       TEXT NOT NULL REFERENCES screens(screen_id),
    tier            TEXT NOT NULL CHECK (tier IN ('tier1','tier2','tier3')),
    cycle_id        TEXT NOT NULL,
    dispatched_at   INTEGER NOT NULL DEFAULT (CAST(unixepoch('subsec')*1000 AS INTEGER)) CHECK (dispatched_at > 1000000000000),
    PRIMARY KEY (screen_id, tier, cycle_id)
) STRICT, WITHOUT ROWID;
CREATE TRIGGER IF NOT EXISTS tier_dispatches_no_clobber
BEFORE INSERT ON tier_dispatches
WHEN EXISTS (SELECT 1 FROM tier_dispatches WHERE screen_id=NEW.screen_id AND tier=NEW.tier AND cycle_id=NEW.cycle_id)
BEGIN SELECT RAISE(ABORT,'dispatch dedup is durable: no re-insert (R8.12)'); END;
CREATE TRIGGER IF NOT EXISTS tier_dispatches_no_delete
BEFORE DELETE ON tier_dispatches
BEGIN SELECT RAISE(ABORT,'dispatch dedup is durable: no delete (R8.12)'); END;

CREATE TABLE IF NOT EXISTS notify_cycles (
    screen_id       TEXT NOT NULL REFERENCES screens(screen_id),
    cycle_id        TEXT NOT NULL,
    notified_at     INTEGER NOT NULL DEFAULT (CAST(unixepoch('subsec')*1000 AS INTEGER)) CHECK (notified_at > 1000000000000),
    PRIMARY KEY (screen_id, cycle_id)
) STRICT, WITHOUT ROWID;
CREATE TRIGGER IF NOT EXISTS notify_cycles_no_clobber
BEFORE INSERT ON notify_cycles
WHEN EXISTS (SELECT 1 FROM notify_cycles WHERE screen_id=NEW.screen_id AND cycle_id=NEW.cycle_id)
BEGIN SELECT RAISE(ABORT,'notify dedup is durable: no re-insert'); END;
CREATE TRIGGER IF NOT EXISTS notify_cycles_no_delete
BEFORE DELETE ON notify_cycles
BEGIN SELECT RAISE(ABORT,'notify dedup is durable: no delete'); END;

-- ---------------------------------------------------------------------------
-- CONSULTS — ONE pending globally, durable against REPLACE (Horizon: REPLACE
-- silently swapped the pending consult).
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS consults (
    consult_id      TEXT NOT NULL PRIMARY KEY,
    screen_id       TEXT REFERENCES screens(screen_id),
    platform        TEXT NOT NULL,
    status          TEXT NOT NULL DEFAULT 'pending'
                    CHECK (status IN ('pending','complete','worker_failed','abandoned')),
    abandon_reason  TEXT,
    payload_dir     TEXT NOT NULL,
    failure_reason  TEXT,
    created_at      INTEGER NOT NULL DEFAULT (CAST(unixepoch('subsec')*1000 AS INTEGER)) CHECK (created_at > 1000000000000),
    resolved_at     INTEGER CHECK (resolved_at IS NULL OR resolved_at > 1000000000000)
) STRICT, WITHOUT ROWID;
CREATE UNIQUE INDEX IF NOT EXISTS ux_one_pending_consult ON consults(status) WHERE status = 'pending';
CREATE TRIGGER IF NOT EXISTS consults_no_clobber
BEFORE INSERT ON consults
WHEN EXISTS (SELECT 1 FROM consults WHERE consult_id = NEW.consult_id)
BEGIN SELECT RAISE(ABORT,'no REPLACE over a consult'); END;
-- v3.1 (Horizon r3 D): the "one pending" invariant must be guarded on VALUE,
-- not just the unique index — a REPLACE via ux_one_pending would implicit-
-- delete the existing pending and swap in a new one. Block a 2nd pending
-- regardless of consult_id; and forbid DELETING a pending consult (a pending
-- consult RESOLVES via status UPDATE, it is never deleted/swapped). Resolved
-- consults may still be cleaned up (delete allowed only when not pending).
CREATE TRIGGER IF NOT EXISTS consults_no_second_pending
BEFORE INSERT ON consults
WHEN NEW.status = 'pending' AND EXISTS (SELECT 1 FROM consults WHERE status = 'pending')
BEGIN SELECT RAISE(ABORT,'ONE consultation at a time (R8.6): a pending consult already exists'); END;
CREATE TRIGGER IF NOT EXISTS consults_pending_not_deletable
BEFORE DELETE ON consults
WHEN OLD.status = 'pending'
BEGIN SELECT RAISE(ABORT,'a pending consult resolves via status, never delete/REPLACE (R8.6)'); END;

-- ---------------------------------------------------------------------------
-- EVENTS — append-only (rowid+AUTOINCREMENT so it stays a rowid table; STRICT).
-- Ordered by event_id, never wall-clock (F14). REPLACE/id-reuse blocked.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS events (
    event_id        INTEGER PRIMARY KEY AUTOINCREMENT,
    kind            TEXT NOT NULL,
    platform        TEXT,
    screen_id       TEXT,
    consult_id      TEXT,
    actor           TEXT NOT NULL CHECK (actor IN ('api','worker','mac','operator','supervisor','system')),
    payload_json    TEXT NOT NULL DEFAULT '{}',
    created_at      INTEGER NOT NULL DEFAULT (CAST(unixepoch('subsec')*1000 AS INTEGER)) CHECK (created_at > 1000000000000)
) STRICT;
CREATE INDEX IF NOT EXISTS idx_events_screen ON events(screen_id, kind, event_id DESC);
CREATE INDEX IF NOT EXISTS idx_events_platform ON events(platform, kind, event_id DESC);

CREATE TABLE IF NOT EXISTS events_archive (
    event_id        INTEGER NOT NULL PRIMARY KEY,
    kind            TEXT NOT NULL,
    platform        TEXT,
    screen_id       TEXT,
    consult_id      TEXT,
    actor           TEXT NOT NULL,
    payload_json    TEXT NOT NULL,
    created_at      INTEGER NOT NULL CHECK (created_at > 1000000000000),
    archived_at     INTEGER NOT NULL DEFAULT (CAST(unixepoch('subsec')*1000 AS INTEGER)) CHECK (archived_at > 1000000000000)
) STRICT, WITHOUT ROWID;

CREATE TRIGGER IF NOT EXISTS events_append_only
BEFORE UPDATE ON events
BEGIN SELECT RAISE(ABORT,'events are append-only'); END;
-- F5 close: faithful copy compares ALL lineage columns NULL-safe (IS), not 5.
CREATE TRIGGER IF NOT EXISTS events_archive_must_match
BEFORE DELETE ON events
WHEN NOT EXISTS (
  SELECT 1 FROM events_archive a WHERE a.event_id = OLD.event_id
    AND a.kind IS OLD.kind AND a.actor IS OLD.actor
    AND a.payload_json IS OLD.payload_json AND a.created_at IS OLD.created_at
    AND a.platform IS OLD.platform AND a.screen_id IS OLD.screen_id
    AND a.consult_id IS OLD.consult_id)
BEGIN SELECT RAISE(ABORT,'archive-before-delete: the archive row must be a faithful copy of ALL lineage'); END;
CREATE TRIGGER IF NOT EXISTS events_archive_append_only
BEFORE UPDATE ON events_archive
BEGIN SELECT RAISE(ABORT,'events_archive is append-only'); END;
CREATE TRIGGER IF NOT EXISTS events_archive_no_delete
BEFORE DELETE ON events_archive
BEGIN SELECT RAISE(ABORT,'events_archive is the floor: nothing is deleted from it'); END;
CREATE TRIGGER IF NOT EXISTS events_no_id_reuse
BEFORE INSERT ON events
WHEN EXISTS (SELECT 1 FROM events_archive WHERE event_id = NEW.event_id)
   OR EXISTS (SELECT 1 FROM events WHERE event_id = NEW.event_id)
BEGIN SELECT RAISE(ABORT,'event_id is unique+retired-once-archived; never reused'); END;
CREATE TRIGGER IF NOT EXISTS events_archive_no_id_reuse
BEFORE INSERT ON events_archive
WHEN EXISTS (SELECT 1 FROM events_archive WHERE event_id = NEW.event_id)
BEGIN SELECT RAISE(ABORT,'archive event_id never reused'); END;
-- v3.1 (Horizon r3 C): guard the VALIDITY of the archive insertion, not just
-- immutability-after. An archive row may only be inserted for a live event
-- whose lineage it faithfully copies (NULL-safe across ALL columns). Closes
-- forged/orphan archive insertion (audit-floor pollution + denial-of-archive).
-- Legit archival: the live event still exists at archive-insert time (the
-- delete follows in the same txn), so this passes for real archival.
CREATE TRIGGER IF NOT EXISTS events_archive_insert_must_match_live
BEFORE INSERT ON events_archive
WHEN NOT EXISTS (
  SELECT 1 FROM events e WHERE e.event_id = NEW.event_id
    AND e.kind IS NEW.kind AND e.actor IS NEW.actor
    AND e.payload_json IS NEW.payload_json AND e.created_at IS NEW.created_at
    AND e.platform IS NEW.platform AND e.screen_id IS NEW.screen_id
    AND e.consult_id IS NEW.consult_id)
BEGIN SELECT RAISE(ABORT,'archive insert must faithfully copy a live event (no forged/orphan archive)'); END;
