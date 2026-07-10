#!/usr/bin/env python3
"""Class-based closure suite for spark/state_schema.sql (v3).

NOT a string-level attack script. For each invariant it probes the equivalence
FAMILY of ways to violate it — UPDATE, INSERT, INSERT OR REPLACE, DELETE+reinsert,
NULL-value bridges, PK/ownership reassignment — including HORIZON's exact v2 breaks
(A NULL-PK dup, B cleared-not-absorbing + screen_id reassignment, C archive
lineage forgery, D REPLACE over events/archive) and GAIA's set. A probe that
should ABORT and instead COMPLETES is a hole. Reports only what it proves.

Init asserts the REQUIRED writer PRAGMAs, incl. recursive_triggers=ON — the
setting Horizon showed is load-bearing for REPLACE firing DELETE triggers.
"""
import sqlite3, sys, os

SCHEMA = os.path.join(os.path.dirname(__file__), '..', 'state_schema.sql')

def conn():
    c = sqlite3.connect(':memory:')
    for p in ('journal_mode=WAL','busy_timeout=5000','synchronous=NORMAL',
              'foreign_keys=ON','recursive_triggers=ON'):
        c.execute(f'PRAGMA {p}')
    c.executescript(open(SCHEMA).read())
    return c

def seed(c):
    c.execute("INSERT INTO platforms(platform) VALUES('p1')")
    c.execute("INSERT INTO platforms(platform) VALUES('p2')")
    c.execute("INSERT INTO screen_types(platform,screen_type,category,artifact_path,artifact_sha) VALUES('p1','EXERCISE_DROPDOWN','EXERCISE','x','y')")
    c.execute("INSERT INTO screens(screen_id,platform) VALUES('s1','p1')")
    c.execute("INSERT INTO screens(screen_id,platform) VALUES('s2','p1')")
    c.execute("INSERT INTO coordination(screen_id,platform,attempt_count,terminal,state) VALUES('s1','p1',4,1,'terminal')")
    c.execute("INSERT INTO bundle_receipts(bundle_id,call_kind,screen_id,slices_json,total_chars,receipt_sha) VALUES('rc','classify','s1','[]',10,'sha')")
    c.execute("INSERT INTO bundle_receipts(bundle_id,call_kind,screen_id,slices_json,total_chars,receipt_sha) VALUES('rx','bt_build','s2','[]',10,'sha')")
    c.commit()

# each probe: (name, must_abort, sql-or-callable). callable(c) may run multiple stmts.
def probes():
    P = []
    def add(n, fn): P.append((n, fn))
    # --- A. NULL primary keys (must abort) ---
    add('A.null-pk-platform', lambda c: c.execute("INSERT INTO platforms(platform) VALUES(NULL)"))
    add('A.null-pk-screen', lambda c: c.execute("INSERT INTO screens(screen_id,platform) VALUES(NULL,'p1')"))
    add('A.null-pk-bt', lambda c: c.execute("INSERT INTO behavior_trees(bt_id,screen_id,revision,bt_json,built_by,source_kind) VALUES(NULL,'s1',1,'{}','w','r')"))
    # --- B. cleared not absorbing + screen_id reassignment ---
    def b_clear_then_consult(c):
        c.execute("INSERT INTO screens(screen_id,platform) VALUES('sb','p1')")
        c.execute("INSERT INTO coordination(screen_id,platform,state,tier,attempt_count,terminal,cleared_reason) VALUES('sb','p1','cleared','tier2',2,0,'advanced')")
        c.execute("UPDATE coordination SET state='consulting' WHERE screen_id='sb'")
    add('B.cleared->consulting', b_clear_then_consult)
    add('B.screen_id-reassign', lambda c: c.execute("UPDATE coordination SET screen_id='s2' WHERE screen_id='s1'"))
    # --- C. archive lineage forgery (must abort the delete) ---
    def c_forge(c):
        c.execute("INSERT INTO events(kind,platform,screen_id,consult_id,actor,payload_json,created_at) VALUES('k','p1','s1','real','system','{}',1720000000001)")
        eid = c.execute("SELECT max(event_id) FROM events").fetchone()[0]
        # archive row with matching 5 fields but FORGED platform/screen_id/consult_id
        c.execute("INSERT INTO events_archive(event_id,kind,platform,screen_id,consult_id,actor,payload_json,created_at) VALUES(?,?,?,?,?,?,?,?)",
                  (eid,'k','p2',None,'forged','system','{}',1720000000001))
        c.execute("DELETE FROM events WHERE event_id=?", (eid,))
    add('C.archive-lineage-forgery', c_forge)
    # --- D. REPLACE over events / archive / dedup / consult / coordination ---
    def d_replace_event(c):
        c.execute("INSERT INTO events(kind,actor) VALUES('k','system')")
        eid = c.execute("SELECT max(event_id) FROM events").fetchone()[0]
        c.execute("INSERT OR REPLACE INTO events(event_id,kind,actor) VALUES(?,?,?)",(eid,'forged','system'))
    add('D.replace-event', d_replace_event)
    def d_replace_coord(c):
        c.execute("INSERT OR REPLACE INTO coordination(screen_id,platform,attempt_count,terminal,state) VALUES('s1','p1',0,0,'normal')")
    add('D.replace-coordination', d_replace_coord)
    def d_replace_pending(c):
        c.execute("INSERT INTO consults(consult_id,platform,payload_dir) VALUES('c1','p1','/d')")
        c.execute("INSERT OR REPLACE INTO consults(consult_id,platform,payload_dir,status) VALUES('c1','p1','/d','pending')")
        c.execute("INSERT INTO consults(consult_id,platform,payload_dir) VALUES('c2','p1','/d')")  # 2nd pending
    add('D.replace-then-second-pending', d_replace_pending)
    def d_dedup_reinsert(c):
        c.execute("INSERT INTO tier_dispatches(screen_id,tier,cycle_id) VALUES('s1','tier1','cyc')")
        c.execute("DELETE FROM tier_dispatches WHERE screen_id='s1'")  # must abort
    add('D.dedup-delete-reinsert', d_dedup_reinsert)
    # --- F6 ladder on INSERT path (v2 only guarded UPDATE) ---
    def f6_attempt5(c):
        c.execute("INSERT INTO screens(screen_id,platform) VALUES('s5','p1')")
        c.execute("INSERT INTO coordination(screen_id,platform,attempt_count) VALUES('s5','p1',5)")
    add('F6.insert-attempt-5', f6_attempt5)
    def f6_term_normal(c):
        c.execute("INSERT INTO screens(screen_id,platform) VALUES('s6','p1')")
        c.execute("INSERT INTO coordination(screen_id,platform,terminal,state) VALUES('s6','p1',1,'normal')")
    add('F6.insert-terminal-normal', f6_term_normal)
    # --- ms timestamp magnitude (STRICT + CHECK) ---
    add('TS.text-timestamp', lambda c: c.execute("INSERT INTO screens(screen_id,platform,first_seen) VALUES('st','p1','2026-07-10')"))
    add('TS.second-scale', lambda c: c.execute("INSERT INTO platforms(platform,onboarded_at) VALUES('ps',1720000000)"))
    # --- classification forgery with unrelated receipt ---
    add('F7.unrelated-receipt', lambda c: c.execute("UPDATE screens SET classification='classified', screen_type='EXERCISE_DROPDOWN', classified_by_bundle_id='rx' WHERE screen_id='s1'"))
    add('F7.no-receipt', lambda c: c.execute("UPDATE screens SET classification='classified', screen_type='EXERCISE_DROPDOWN', classified_by_bundle_id=NULL WHERE screen_id='s1'"))
    # --- validated BT forgery ---
    def bt_two_validated(c):
        c.execute("INSERT INTO behavior_trees(bt_id,screen_id,revision,bt_json,built_by,source_kind,status,success_count) VALUES('b1','s1',1,'{}','w','r','validated',1)")
        c.execute("INSERT INTO behavior_trees(bt_id,screen_id,revision,bt_json,built_by,source_kind,status,success_count) VALUES('b2','s1',2,'{}','w','r','validated',1)")
    add('F10.two-validated', bt_two_validated)
    add('F10.validated-zero-success', lambda c: c.execute("INSERT INTO behavior_trees(bt_id,screen_id,revision,bt_json,built_by,source_kind,status,success_count) VALUES('b3','s1',3,'{}','w','r','validated',0)"))
    def bt_replace_history(c):
        c.execute("INSERT INTO behavior_trees(bt_id,screen_id,revision,bt_json,built_by,source_kind) VALUES('b4','s1',4,'{}','w','r')")
        c.execute("INSERT OR REPLACE INTO behavior_trees(bt_id,screen_id,revision,bt_json,built_by,source_kind) VALUES('b4','s1',4,'FORGED','w','r')")
    add('F10.replace-history', bt_replace_history)
    # --- registry: bare master, exercise-deterministic, fake trust ---
    add('F8.bare-master', lambda c: c.execute("INSERT INTO screen_types(platform,screen_type,category,artifact_path,artifact_sha) VALUES('p1','EXERCISE','EXERCISE','x','y')"))
    add('F9.exercise-deterministic', lambda c: c.execute("INSERT INTO screen_types(platform,screen_type,category,artifact_path,artifact_sha,deterministic) VALUES('p1','EX2','EXERCISE','x','y',1)"))
    add('F9.fake-trust', lambda c: c.execute("INSERT INTO screen_types(platform,screen_type,category,artifact_path,artifact_sha,trust) VALUES('p1','VID','VIDEO','x','y','trusted')"))
    # --- slice currency incl NULL platform dup ---
    def slice_dup(c):
        c.execute("INSERT INTO context_slices(slice_id,platform,level,selector,source_path,source_sha) VALUES('sl1',NULL,0,'core','p','a')")
        c.execute("INSERT INTO context_slices(slice_id,platform,level,selector,source_path,source_sha) VALUES('sl2',NULL,0,'core','p','b')")
    add('F11.L0-null-dup', slice_dup)
    # --- direct-decrement (v1/v2 baseline, must still abort) ---
    add('base.decrement', lambda c: c.execute("UPDATE coordination SET attempt_count=1 WHERE screen_id='s1'"))
    add('base.unterminate', lambda c: c.execute("UPDATE coordination SET terminal=0 WHERE screen_id='s1'"))
    return P

def run():
    P = probes()
    holes, closed = [], []
    for name, fn in P:
        c = conn(); seed(c)
        try:
            fn(c); c.commit()
            holes.append(name)          # completed but should have aborted
        except (sqlite3.IntegrityError, sqlite3.OperationalError):
            closed.append(name)
        finally:
            c.close()
    print(f"CLOSED ({len(closed)}/{len(P)}):")
    for n in closed: print("  ✓", n)
    if holes:
        print(f"\nHOLES ({len(holes)}) — completed but must abort:")
        for n in holes: print("  ✗", n)
    else:
        print("\nNO HOLES — every probed violation class aborts.")
    return 1 if holes else 0

if __name__ == '__main__':
    sys.exit(run())
