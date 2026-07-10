#!/usr/bin/env python3
"""Class-based closure suite for spark/state_schema.sql (v3.1).

CORRECTED after HORIZON r3 caught a FALSE-CLOSURE in the earlier version: a
multi-statement probe whose SETUP was the violation and whose final statement
aborted was scored "closed" without checking the setup didn't persist.

Model now:
  probe = (name, setup(c), violation_sql_or_fn, residue_check(c)->bool)
  * setup runs and COMMITS (must be legal state).
  * violation is the SINGLE operation that MUST abort.
  * a probe is CLOSED iff: (violation raised) AND (residue_check confirms the
    forbidden mutation did NOT persist). Both required — an abort with residue
    is a HOLE, not a closure.
Attacks the equivalence FAMILY incl. ALTERNATE-UNIQUE REPLACE targets (not just
PK), per Horizon r3. Asserts recursive_triggers=ON (load-bearing for REPLACE).
"""
import sqlite3, os, sys, tempfile, time
from pathlib import Path

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

SCHEMA = os.path.join(os.path.dirname(__file__), '..', 'state_schema.sql')

def conn():
    c = sqlite3.connect(':memory:')
    for p in ('foreign_keys=ON','recursive_triggers=ON'):
        c.execute(f'PRAGMA {p}')
    c.executescript(open(SCHEMA).read())
    return c

def seed(c):
    c.execute("INSERT INTO platforms(platform) VALUES('p1')")
    c.execute("INSERT INTO platforms(platform) VALUES('p2')")
    c.execute("INSERT INTO screen_types(platform,screen_type,category,artifact_path,artifact_sha) VALUES('p1','EXERCISE_DROPDOWN','EXERCISE','x','y')")
    for s in ('s1','s2'):
        c.execute("INSERT INTO screens(screen_id,platform) VALUES(?,?)",(s,'p1'))
    c.execute("INSERT INTO coordination(screen_id,platform,attempt_count,terminal,state) VALUES('s1','p1',4,1,'terminal')")
    c.execute("INSERT INTO bundle_receipts(bundle_id,call_kind,screen_id,slices_json,total_chars,receipt_sha) VALUES('rc','classify','s1','[]',10,'sha')")
    c.execute("INSERT INTO bundle_receipts(bundle_id,call_kind,screen_id,slices_json,total_chars,receipt_sha) VALUES('rx','bt_build','s2','[]',10,'sha')")
    c.commit()

# residue helpers
def no_row(c, sql, args=()): return c.execute(sql, args).fetchone() is None
def val(c, sql, args=()):
    r = c.execute(sql, args).fetchone(); return r[0] if r else None

PROBES = []
def P(name, setup, violation, residue):
    PROBES.append((name, setup, violation, residue))

# --- A: NULL / empty logical PKs ---
P('A.null-pk', None, ("INSERT INTO screens(screen_id,platform) VALUES(NULL,'p1')", ()),
  lambda c: no_row(c,"SELECT 1 FROM screens WHERE screen_id IS NULL"))

# --- B: cleared class (full) ---
def b_setup(c):
    c.execute("INSERT INTO screens(screen_id,platform) VALUES('sb','p1')")
    c.execute("INSERT INTO coordination(screen_id,platform,state,attempt_count,terminal,cleared_reason) VALUES('sb','p1','cleared',2,0,'advanced')")
    c.commit()
P('B.cleared->consulting', b_setup, ("UPDATE coordination SET state='consulting' WHERE screen_id='sb'",()),
  lambda c: val(c,"SELECT state FROM coordination WHERE screen_id='sb'")=='cleared')
P('B.cleared-mutate-field-while-cleared', b_setup, ("UPDATE coordination SET tier='tier3', attempt_count=4, platform='p2' WHERE screen_id='sb'",()),
  lambda c: val(c,"SELECT tier FROM coordination WHERE screen_id='sb'") is None and val(c,"SELECT attempt_count FROM coordination WHERE screen_id='sb'")==2)
P('B.screen_id-reassign', None, ("UPDATE coordination SET screen_id='s2' WHERE screen_id='s1'",()),
  lambda c: no_row(c,"SELECT 1 FROM coordination WHERE screen_id='s2'"))
# and the LEGAL re-arm of a cleared terminal ladder must SUCCEED (liveness, not a hole)

# --- C: archive insertion validity + faithful delete ---
def c_setup(c):
    c.execute("INSERT INTO events(kind,platform,screen_id,consult_id,actor,payload_json,created_at) VALUES('k','p1','s1','real','system','{}',1720000000001)")
    c.commit()
P('C.forge-orphan-archive', c_setup, ("INSERT INTO events_archive(event_id,kind,platform,screen_id,consult_id,actor,payload_json,created_at) VALUES(999,'forged','p2',NULL,'fake','system','{}',1720000000001)",()),
  lambda c: no_row(c,"SELECT 1 FROM events_archive WHERE event_id=999"))
P('C.forge-mismatched-archive', c_setup, ("INSERT INTO events_archive(event_id,kind,platform,screen_id,consult_id,actor,payload_json,created_at) SELECT event_id,'forged','p2',NULL,'fake',actor,payload_json,created_at FROM events WHERE screen_id='s1'",()),
  lambda c: no_row(c,"SELECT 1 FROM events_archive WHERE kind='forged'"))
P('C.delete-live-no-archive', c_setup, ("DELETE FROM events WHERE screen_id='s1'",()),
  lambda c: not no_row(c,"SELECT 1 FROM events WHERE screen_id='s1'"))

# --- D: REPLACE via ALTERNATE unique targets (the r3 class) ---
def d_bt_setup(c):
    c.execute("INSERT INTO behavior_trees(bt_id,screen_id,revision,bt_json,built_by,source_kind) VALUES('b1','s1',1,'ORIGINAL','worker','recipe')")
    c.commit()
P('D.bt-replace-alt-unique', d_bt_setup,
  ("INSERT OR REPLACE INTO behavior_trees(bt_id,screen_id,revision,bt_json,built_by,source_kind) VALUES('b2','s1',1,'FORGED','worker','recipe')",()),
  lambda c: val(c,"SELECT bt_json FROM behavior_trees WHERE screen_id='s1' AND revision=1")=='ORIGINAL' and no_row(c,"SELECT 1 FROM behavior_trees WHERE bt_id='b2'"))
def d_pending_setup(c):
    c.execute("INSERT INTO consults(consult_id,platform,payload_dir,status) VALUES('c1','p1','/d','pending')")
    c.commit()
P('D.consult-replace-alt-pending', d_pending_setup,
  ("INSERT OR REPLACE INTO consults(consult_id,platform,payload_dir,status) VALUES('c2','p1','/d','pending')",()),
  lambda c: val(c,"SELECT consult_id FROM consults WHERE status='pending'")=='c1' and val(c,"SELECT count(*) FROM consults WHERE status='pending'")==1)
P('D.second-pending-plain', d_pending_setup,
  ("INSERT INTO consults(consult_id,platform,payload_dir,status) VALUES('c3','p1','/d','pending')",()),
  lambda c: val(c,"SELECT count(*) FROM consults WHERE status='pending'")==1)
def d_slice_setup(c):
    c.execute("INSERT INTO context_slices(slice_id,platform,level,selector,source_path,source_sha) VALUES('sl1',NULL,0,'core','p','ORIG')")
    c.commit()
P('D.slice-replace-current', d_slice_setup,
  ("INSERT OR REPLACE INTO context_slices(slice_id,platform,level,selector,source_path,source_sha) VALUES('sl2',NULL,0,'core','p','FORGED')",()),
  lambda c: val(c,"SELECT source_sha FROM context_slices WHERE selector='core' AND trust<>'superseded'")=='ORIG' and no_row(c,"SELECT 1 FROM context_slices WHERE slice_id='sl2'"))
P('D.dedup-delete-reinsert', lambda c:(c.execute("INSERT INTO tier_dispatches(screen_id,tier,cycle_id) VALUES('s1','tier1','cyc')"),c.commit()),
  ("DELETE FROM tier_dispatches WHERE screen_id='s1'",()),
  lambda c: not no_row(c,"SELECT 1 FROM tier_dispatches WHERE screen_id='s1'"))
P('D.coord-replace', None, ("INSERT OR REPLACE INTO coordination(screen_id,platform,attempt_count,terminal,state) VALUES('s1','p1',0,0,'normal')",()),
  lambda c: val(c,"SELECT attempt_count FROM coordination WHERE screen_id='s1'")==4)
P('D.event-replace', lambda c:(c.execute("INSERT INTO events(kind,actor) VALUES('k','system')"),c.commit()),
  ("INSERT OR REPLACE INTO events(event_id,kind,actor) VALUES((SELECT max(event_id) FROM events),'forged','system')",()),
  lambda c: no_row(c,"SELECT 1 FROM events WHERE kind='forged'"))

# --- F6 ladder on INSERT ---
P('F6.insert-attempt-5', lambda c:(c.execute("INSERT INTO screens(screen_id,platform) VALUES('s5','p1')"),c.commit()),
  ("INSERT INTO coordination(screen_id,platform,attempt_count) VALUES('s5','p1',5)",()),
  lambda c: no_row(c,"SELECT 1 FROM coordination WHERE screen_id='s5'"))
P('F6.insert-terminal-normal', lambda c:(c.execute("INSERT INTO screens(screen_id,platform) VALUES('s6','p1')"),c.commit()),
  ("INSERT INTO coordination(screen_id,platform,terminal,state) VALUES('s6','p1',1,'normal')",()),
  lambda c: no_row(c,"SELECT 1 FROM coordination WHERE screen_id='s6'"))

# --- timestamps / classification / trust / bt (single-statement violations) ---
P('TS.text-ts', None, ("INSERT INTO screens(screen_id,platform,first_seen) VALUES('st','p1','2026')",()), lambda c: no_row(c,"SELECT 1 FROM screens WHERE screen_id='st'"))
P('TS.second-scale', None, ("INSERT INTO platforms(platform,onboarded_at) VALUES('ps',1720000000)",()), lambda c: no_row(c,"SELECT 1 FROM platforms WHERE platform='ps'"))
P('F7.unrelated-receipt', None, ("UPDATE screens SET classification='classified',screen_type='EXERCISE_DROPDOWN',classified_by_bundle_id='rx' WHERE screen_id='s1'",()), lambda c: val(c,"SELECT classification FROM screens WHERE screen_id='s1'")=='pending')
P('F8.bare-master', None, ("INSERT INTO screen_types(platform,screen_type,category,artifact_path,artifact_sha) VALUES('p1','EXERCISE','EXERCISE','x','y')",()), lambda c: no_row(c,"SELECT 1 FROM screen_types WHERE screen_type='EXERCISE'"))
P('F9.exercise-deterministic', None, ("INSERT INTO screen_types(platform,screen_type,category,artifact_path,artifact_sha,deterministic) VALUES('p1','EX2','EXERCISE','x','y',1)",()), lambda c: no_row(c,"SELECT 1 FROM screen_types WHERE screen_type='EX2'"))
P('F9.fake-trust', None, ("INSERT INTO screen_types(platform,screen_type,category,artifact_path,artifact_sha,trust) VALUES('p1','VID','VIDEO','x','y','trusted')",()), lambda c: no_row(c,"SELECT 1 FROM screen_types WHERE screen_type='VID'"))
P('F10.validated-zero', None, ("INSERT INTO behavior_trees(bt_id,screen_id,revision,bt_json,built_by,source_kind,status,success_count) VALUES('bz','s1',7,'{}','w','r','validated',0)",()), lambda c: no_row(c,"SELECT 1 FROM behavior_trees WHERE bt_id='bz'"))
P('base.decrement', None, ("UPDATE coordination SET attempt_count=1 WHERE screen_id='s1'",()), lambda c: val(c,"SELECT attempt_count FROM coordination WHERE screen_id='s1'")==4)
P('base.unterminate', None, ("UPDATE coordination SET terminal=0 WHERE screen_id='s1'",()), lambda c: val(c,"SELECT terminal FROM coordination WHERE screen_id='s1'")==1)

# --- Horizon r4: UPSERT (ON CONFLICT DO UPDATE) path, slice_blobs REPLACE,
#     cleared-frozen omitted columns, tier NULL-bridge, timestamp floor gaps ---
P('r4A.slice-upsert-rewrite', d_slice_setup,
  ("INSERT INTO context_slices(slice_id,platform,level,selector,source_path,source_sha) VALUES('sl2',NULL,0,'core','evil','FORGED') ON CONFLICT(ifnull(platform,'*'),level,selector) WHERE trust<>'superseded' DO UPDATE SET source_path=excluded.source_path, source_sha=excluded.source_sha",()),
  lambda c: val(c,"SELECT source_sha FROM context_slices WHERE selector='core' AND trust<>'superseded'")=='ORIG')
P('r4B.bt-upsert-provenance', d_bt_setup,
  ("INSERT INTO behavior_trees(bt_id,screen_id,revision,bt_json,built_by,source_kind,type_artifact_sha,created_at) VALUES('b2','s1',1,'NEW','worker','recipe','TYPE_FORGED',1720000000009) ON CONFLICT(screen_id,revision) DO UPDATE SET type_artifact_sha=excluded.type_artifact_sha",()),
  lambda c: no_row(c,"SELECT 1 FROM behavior_trees WHERE type_artifact_sha='TYPE_FORGED'"))
def r4c_setup(c):
    c.execute("INSERT INTO slice_blobs(sha,body,bytes) VALUES('h','ORIGINAL',8)"); c.commit()
P('r4C.slice_blobs-replace', r4c_setup,
  ("INSERT OR REPLACE INTO slice_blobs(sha,body,bytes) VALUES('h','FORGED',6)",()),
  lambda c: val(c,"SELECT body FROM slice_blobs WHERE sha='h'")=='ORIGINAL')
P('r4D.cleared-mutate-noncore', b_setup,
  ("UPDATE coordination SET response_pending_until=1720000009999, yaml_sha_at_attempt='FORGED', user_instructions='FORGED' WHERE screen_id='sb'",()),
  lambda c: no_row(c,"SELECT 1 FROM coordination WHERE screen_id='sb' AND yaml_sha_at_attempt='FORGED'"))
def r4e_setup(c):
    c.execute("INSERT INTO screens(screen_id,platform) VALUES('se','p1')")
    c.execute("INSERT INTO coordination(screen_id,platform,state,tier,attempt_count) VALUES('se','p1','diagnosing','tier3',3)")
    c.commit()
# The NULL bridge (tier=NULL then tier=tier1) is closed by blocking step 1:
# tier->NULL must abort outside the re-arm, so the bridge can't even begin.
P('r4E.tier-null-bridge', r4e_setup,
  ("UPDATE coordination SET tier=NULL WHERE screen_id='se'",()),
  lambda c: val(c,"SELECT tier FROM coordination WHERE screen_id='se'")=='tier3')
P('r4F.event-second-scale-ts', None,
  ("INSERT INTO events(kind,actor,created_at) VALUES('k','system',1720000000)",()),
  lambda c: no_row(c,"SELECT 1 FROM events WHERE created_at=1720000000"))

# --- Horizon r5: enter-cleared smuggle, dedup UPDATE, empty-string PK, screens REPLACE ---
P('r5_1.enter-cleared-smuggle', None,
  ("UPDATE coordination SET state='cleared',cleared_reason='advanced',response_pending_until=1720000009999,yaml_sha_at_attempt='FORGED',user_instructions='FORGED' WHERE screen_id='s1'",()),
  lambda c: no_row(c,"SELECT 1 FROM coordination WHERE screen_id='s1' AND yaml_sha_at_attempt='FORGED'"))
def r5_2_setup(c):
    c.execute("INSERT INTO tier_dispatches(screen_id,tier,cycle_id) VALUES('s1','tier1','cyc')"); c.commit()
P('r5_2.dedup-update-frees-key', r5_2_setup,
  ("UPDATE tier_dispatches SET tier='tier2' WHERE screen_id='s1' AND tier='tier1' AND cycle_id='cyc'",()),
  lambda c: val(c,"SELECT tier FROM tier_dispatches WHERE screen_id='s1'")=='tier1')
P('r5_3.empty-string-pk', None, ("INSERT INTO screens(screen_id,platform) VALUES('','p1')",()),
  lambda c: no_row(c,"SELECT 1 FROM screens WHERE screen_id=''"))
P('r5_3.whitespace-pk', None, ("INSERT INTO screens(screen_id,platform) VALUES('   ','p1')",()),
  lambda c: no_row(c,"SELECT 1 FROM screens WHERE trim(screen_id)=''"))
P('r5_4.screens-replace', None, ("INSERT OR REPLACE INTO screens(screen_id,platform) VALUES('s2','p2')",()),
  lambda c: val(c,"SELECT platform FROM screens WHERE screen_id='s2'")=='p1')
P('r5_4.screens-delete', None, ("DELETE FROM screens WHERE screen_id='s2'",()),
  lambda c: not no_row(c,"SELECT 1 FROM screens WHERE screen_id='s2'"))

def run():
    closed, holes, false_closures = [], [], []
    for name, setup, violation, residue in PROBES:
        c = conn(); seed(c)
        try:
            if setup: setup(c)
        except Exception as e:
            holes.append(f"{name} (SETUP FAILED: {str(e)[:40]})"); c.close(); continue
        aborted = False
        try:
            sql, args = violation if isinstance(violation, tuple) else (violation, ())
            c.execute(sql, args); c.commit()
        except (sqlite3.IntegrityError, sqlite3.OperationalError):
            aborted = True
            c.rollback()
        # oracle: closed IFF aborted AND no residue
        try:
            clean = residue(c)
        except Exception:
            clean = False
        if aborted and clean:
            closed.append(name)
        elif aborted and not clean:
            false_closures.append(name)   # aborted but the forbidden mutation persisted
        else:
            holes.append(name)            # completed
        c.close()
    print(f"CLOSED ({len(closed)}/{len(PROBES)}):"); [print("  ✓", n) for n in closed]
    if false_closures:
        print(f"\nFALSE-CLOSURES ({len(false_closures)}) — aborted but residue persisted:"); [print("  ⚠", n) for n in false_closures]
    if holes:
        print(f"\nHOLES ({len(holes)}) — violation completed:"); [print("  ✗", n) for n in holes]
    if not holes and not false_closures:
        print("\nNO HOLES, NO FALSE-CLOSURES — every violation class aborts AND leaves no residue.")
    return 1 if (holes or false_closures) else 0

# --- LIVENESS: legal operations MUST succeed (a closed-but-dead schema is also
# a failure). Run: python state_closure_suite.py --liveness
def liveness():
    c = conn()
    c.execute("INSERT INTO platforms(platform) VALUES('p1')")
    c.execute("INSERT INTO screens(screen_id,platform) VALUES('s1','p1')")
    c.execute("INSERT INTO coordination(screen_id,platform,attempt_count,terminal,state) VALUES('s1','p1',4,1,'terminal')")
    c.execute("UPDATE coordination SET state='cleared',cleared_reason='user_stop_reset',terminal=0,attempt_count=0 WHERE screen_id='s1'")
    checks = [
      ("re-arm cleared->normal", "UPDATE coordination SET state='normal',attempt_count=0,terminal=0,tier=NULL WHERE screen_id='s1'"),
      ("climb 0->1", "UPDATE coordination SET attempt_count=1,tier='tier1',state='diagnosing' WHERE screen_id='s1'"),
      ("climb tier1->tier2", "UPDATE coordination SET attempt_count=2,tier='tier2' WHERE screen_id='s1'"),
      ("clear on advance", "UPDATE coordination SET state='cleared',cleared_reason='advanced' WHERE screen_id='s1'"),
    ]
    bad=[]
    for n,sql in checks:
        try: c.execute(sql)
        except Exception as e: bad.append(f"{n}: {e}")
    print("LIVENESS:", "OK" if not bad else "OVER-BLOCKED")
    for b in bad: print("  ✗", b)
    app_bad = app_liveness()
    return 1 if (bad or app_bad) else 0


def app_liveness():
    from spark.state_db import init_state_db
    from spark.state_repo import StateRepo

    bad = []
    with tempfile.TemporaryDirectory() as tmp:
        repo = StateRepo(init_state_db(Path(tmp) / "state.db"))
        evidence = {"source": "state_closure_suite.app_liveness"}
        try:
            first = repo.record_escalation_attempt(
                platform="p1",
                screen_hash="screen-a",
                consult_id="consult_a",
                actor="api",
                evidence=evidence,
            )
            if first != 1:
                bad.append(f"first distinct consult attempt: got {first}")
            first_state = repo.get_ladder_state(platform="p1", screen_hash="screen-a")
            first_attempt_at = first_state.get("last_attempt_at_ms")
            if not first_attempt_at:
                bad.append("first attempt timestamp missing")
            time.sleep(0.01)
            repo.start_diagnosis_cycle(
                platform="p1",
                screen_hash="screen-a",
                tier="tier1",
                resume_at_ms=int((time.time() + 0.01) * 1000),
                actor="api",
                evidence=evidence,
            )
            time.sleep(0.01)
            if not repo.resume_diagnosis_cycle(
                platform="p1",
                screen_hash="screen-a",
                actor="api",
                evidence=evidence,
            ):
                bad.append("resume diagnosis cycle returned false")
            resumed_state = repo.get_ladder_state(platform="p1", screen_hash="screen-a")
            if resumed_state.get("attempt") != 1:
                bad.append(f"resume climbed attempt count to {resumed_state.get('attempt')}")
            if resumed_state.get("last_attempt_at_ms") != first_attempt_at:
                bad.append("resume rewrote last real attempt timestamp")
            empty = repo.record_escalation_attempt(
                platform="p1",
                screen_hash="screen-a",
                consult_id="",
                actor="api",
                evidence=evidence,
            )
            if empty != 1:
                bad.append(f"empty consult id climbed attempt count to {empty}")
            same = repo.record_escalation_attempt(
                platform="p1",
                screen_hash="screen-a",
                consult_id="consult_a",
                actor="api",
                evidence=evidence,
            )
            if same != 1:
                bad.append(f"same consult id climbed attempt count to {same}")
            second = repo.record_escalation_attempt(
                platform="p1",
                screen_hash="screen-a",
                consult_id="consult_b",
                actor="api",
                evidence=evidence,
            )
            if second != 2:
                bad.append(f"second distinct consult did not climb to 2 (got {second})")

            repo.clear_ladder(
                platform="p1",
                screen_hash="screen-a",
                reason="yaml_fold",
                actor="api",
                evidence=evidence,
            )
            folded = repo.get_ladder_state(platform="p1", screen_hash="screen-a")
            if folded.get("state") != "cleared" or folded.get("attempt") != 0 or folded.get("terminal"):
                bad.append(f"non-terminal yaml fold did not clear cleanly: {folded}")
            rearmed = repo.record_escalation_attempt(
                platform="p1",
                screen_hash="screen-a",
                consult_id="consult_c",
                actor="api",
                evidence=evidence,
            )
            if rearmed != 1:
                bad.append(f"yaml-fold re-arm did not start clean ladder at 1 (got {rearmed})")

            repo.mark_terminal(
                platform="p1",
                screen_hash="screen-b",
                actor="api",
                evidence=evidence,
            )
            repo.clear_ladder(
                platform="p1",
                screen_hash="screen-b",
                reason="yaml_fold",
                actor="api",
                evidence=evidence,
            )
            terminal = repo.get_ladder_state(platform="p1", screen_hash="screen-b")
            if terminal.get("state") != "terminal" or not terminal.get("terminal"):
                bad.append(f"terminal yaml fold un-terminated screen: {terminal}")
            ignored = repo.record_escalation_attempt(
                platform="p1",
                screen_hash="screen-b",
                consult_id="consult_terminal",
                actor="api",
                evidence=evidence,
            )
            if ignored < 4:
                bad.append(f"terminal attempt returned non-terminal count {ignored}")
        except Exception as exc:
            bad.append(f"app liveness exception: {exc}")
    print("APP-LIVENESS:", "OK" if not bad else "OVER-BLOCKED")
    for b in bad: print("  ✗", b)
    return bad

if __name__ == '__main__':
    sys.exit(liveness() if '--liveness' in sys.argv else run())
