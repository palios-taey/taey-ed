# Repo Hygiene Report - cleanup-repo-hygiene

Produced by taey-ed-codex on 2026-07-09 from branch `peer/codex-repo-hygiene`.

## Summary

- Observed: item 1 code commit `5cfb76f` moves new escalation packets, review routing, and UNSOLVED logging under `DATA_DIR/consultations`, with production fail-loud when `TAEY_ED_DATA_DIR` is unset.
- Observed: item 2 removed untracked `spark_v2/` (504K; pycache plus `spark_v2/platforms/khan_academy/provisional_knowledge.json`) and default-generated `.claude/skills/gitnexus/` from this worktree. No tracked commit because both were ignored/untracked.
- Observed: item 3 deleted local branch `subtype-cutover` with `git branch -d`; `taey-ed-grok` and `scr1-yaml-coverage-gemini` were left untouched and still attached to peer worktrees.
- Observed: item 4 inspected both main-checkout stashes. Both are superseded GitNexus metadata refreshes against `AGENTS.md` and `CLAUDE.md`; current main already has newer `taey-ed` GitNexus metadata. No rescue branch was created; stashes were not dropped.
- Observed: item 5 found both live systemd services active/running with `NeedDaemonReload=yes`; diffs are recorded below. No daemon reload was run.
- Observed: item 6 docs scan found no README/docs references to the seven removed APIs (`store_variant_bt`, `is_non_deterministic`, `get_stats`, `bump`, `attempt`, `load_learned`, `get_quirks_for_screen`) and no old blind-settle/dropdown flow wording. Commit `1a2ad7d` corrects stale `TAEY_ED_MODE` wording in `deploy/systemd/README.md`.

## Item 1 Verification

Commands:

```bash
python3 -m py_compile spark/tasks/paths.py spark/tasks/escalation.py spark/routes/next_action.py spark/tasks/consultation_request.py
TAEY_ED_PRODUCTION=1 python3 - <<'PY'
try:
    import spark.tasks.paths  # noqa: F401
except RuntimeError as exc:
    print(exc)
else:
    raise SystemExit('expected RuntimeError when TAEY_ED_DATA_DIR is unset in production')
PY
TAEY_ED_DATA_DIR=/home/mira/taey-ed-data /home/mira/taey-ed/.venv/bin/python3 - <<'PY'
import json
import tempfile
from pathlib import Path

import spark.server  # noqa: F401
from spark.tasks.escalation import build_packet
from spark.tasks.paths import DATA_DIR

with tempfile.TemporaryDirectory(prefix="taey-ed-codex-oracle-") as tmp:
    src = Path(tmp)
    (src / "tree.json").write_text(json.dumps({"role": "AXWebArea", "name": "codex repo hygiene oracle", "children": []}))
    packet_path = build_packet(
        platform="codex_repo_hygiene_oracle",
        screen_hash="abc123def4567890abc123def4567890",
        consult_path=src,
        diag_state_dir=src,
        retry_count=0,
        knowledge={},
        operational_notes_rendered="oracle probe",
        screen_type_hint="UNKNOWN",
        specific_ask="Repo hygiene oracle: verify escalation packet root only.",
    )

expected_root = DATA_DIR / "consultations" / "ESCALATIONS"
if expected_root not in packet_path.parents or not packet_path.exists():
    raise SystemExit(f"packet oracle failed: {packet_path}")
print(f"packet_path={packet_path}")
PY
```

Observed oracle packet:

```text
/home/mira/taey-ed-data/consultations/ESCALATIONS/ESC_codex_repo_hygiene_oracle_abc123def4567890_20260709T194609Z/packet.md
```

Observed first error during oracle: system Python lacked `bcrypt`; deployed venv `/home/mira/taey-ed/.venv/bin/python3` imports `bcrypt` successfully and was used for the service import-chain oracle.

## Item 4 Stash Diffs

Main repo status before inspection:

```text
## main
```

Stash list:

```text
stash@{2026-05-15 14:28:26 +0000}: On peer/taey-ed-codex: pre-rebase-metadata-refresh
stash@{2026-05-15 14:21:55 +0000}: On fix/autonomous-loop-recovery: pre-cherrypick-stash
```

### stash@{0}

```diff
diff --git a/AGENTS.md b/AGENTS.md
index 2a4f0c1..a0be7dd 100644
--- a/AGENTS.md
+++ b/AGENTS.md
@@ -1,7 +1,7 @@
 <!-- gitnexus:start -->
 # GitNexus - Code Intelligence

-This project is indexed by GitNexus as **taey-ed** (808 symbols, 2156 relationships, 63 execution flows). Use the GitNexus MCP tools to understand code, assess impact, and navigate safely.
+This project is indexed by GitNexus as **taey-ed-codex** (991 symbols, 2593 relationships, 76 execution flows). Use the GitNexus MCP tools to understand code, assess impact, and navigate safely.

 > If any GitNexus tool warns the index is stale, run `npx gitnexus analyze` in terminal first.

@@ -17,7 +17,7 @@ This project is indexed by GitNexus as **taey-ed** (808 symbols, 2156 relationsh

 1. `gitnexus_query({query: "<error or symptom>"})` - find execution flows related to the issue
 2. `gitnexus_context({name: "<suspect function>"})` - see all callers, callees, and process participation
-3. `READ gitnexus://repo/taey-ed/process/{processName}` - trace the full execution flow step by step
+3. `READ gitnexus://repo/taey-ed-codex/process/{processName}` - trace the full execution flow step by step
 4. For regressions: `gitnexus_detect_changes({scope: "compare", base_ref: "main"})` - see what your branch changed

 ## When Refactoring
@@ -56,10 +56,10 @@ This project is indexed by GitNexus as **taey-ed** (808 symbols, 2156 relationsh

 | Resource | Use for |
 |----------|---------|
-| `gitnexus://repo/taey-ed/context` | Codebase overview, check index freshness |
-| `gitnexus://repo/taey-ed/clusters` | All functional areas |
-| `gitnexus://repo/taey-ed/processes` | All execution flows |
-| `gitnexus://repo/taey-ed/process/{name}` | Step-by-step execution trace |
+| `gitnexus://repo/taey-ed-codex/context` | Codebase overview, check index freshness |
+| `gitnexus://repo/taey-ed-codex/clusters` | All functional areas |
+| `gitnexus://repo/taey-ed-codex/processes` | All execution flows |
+| `gitnexus://repo/taey-ed-codex/process/{name}` | Step-by-step execution trace |

 ## Self-Check Before Finishing

@@ -98,4 +98,4 @@ To check whether embeddings exist, inspect `.gitnexus/meta.json` - the `stats.
 | Tools, resources, schema reference | `.claude/skills/gitnexus/gitnexus-guide/SKILL.md` |
 | Index, status, clean, wiki CLI commands | `.claude/skills/gitnexus/gitnexus-cli/SKILL.md` |

-<!-- gitnexus:end -->
\ No newline at end of file
+<!-- gitnexus:end -->
diff --git a/CLAUDE.md b/CLAUDE.md
index 0159312..b2df7a0 100644
--- a/CLAUDE.md
+++ b/CLAUDE.md
@@ -293,7 +293,7 @@ Three-register truth on every claim: **Observed** (verified) / **Inferred** (pat
 <!-- gitnexus:start -->
 # GitNexus - Code Intelligence

-This project is indexed by GitNexus as **taey-ed** (808 symbols, 2156 relationships, 63 execution flows). Use the GitNexus MCP tools to understand code, assess impact, and navigate safely.
+This project is indexed by GitNexus as **taey-ed-codex** (991 symbols, 2593 relationships, 76 execution flows). Use the GitNexus MCP tools to understand code, assess impact, and navigate safely.

 > If any GitNexus tool warns the index is stale, run `npx gitnexus analyze` in terminal first.

@@ -309,7 +309,7 @@ This project is indexed by GitNexus as **taey-ed** (808 symbols, 2156 relationsh

 1. `gitnexus_query({query: "<error or symptom>"})` - find execution flows related to the issue
 2. `gitnexus_context({name: "<suspect function>"})` - see all callers, callees, and process participation
-3. `READ gitnexus://repo/taey-ed/process/{processName}` - trace the full execution flow step by step
+3. `READ gitnexus://repo/taey-ed-codex/process/{processName}` - trace the full execution flow step by step
 4. For regressions: `gitnexus_detect_changes({scope: "compare", base_ref: "main"})` - see what your branch changed

 ## When Refactoring
@@ -348,10 +348,10 @@ This project is indexed by GitNexus as **taey-ed** (808 symbols, 2156 relationsh

 | Resource | Use for |
 |----------|---------|
-| `gitnexus://repo/taey-ed/context` | Codebase overview, check index freshness |
-| `gitnexus://repo/taey-ed/clusters` | All functional areas |
-| `gitnexus://repo/taey-ed/processes` | All execution flows |
-| `gitnexus://repo/taey-ed/process/{name}` | Step-by-step execution trace |
+| `gitnexus://repo/taey-ed-codex/context` | Codebase overview, check index freshness |
+| `gitnexus://repo/taey-ed-codex/clusters` | All functional areas |
+| `gitnexus://repo/taey-ed-codex/processes` | All execution flows |
+| `gitnexus://repo/taey-ed-codex/process/{name}` | Step-by-step execution trace |

 ## Self-Check Before Finishing
```

### stash@{1}

```diff
diff --git a/AGENTS.md b/AGENTS.md
index 2a4f0c1..e70a6f2 100644
--- a/AGENTS.md
+++ b/AGENTS.md
@@ -1,7 +1,7 @@
 <!-- gitnexus:start -->
 # GitNexus - Code Intelligence

-This project is indexed by GitNexus as **taey-ed** (808 symbols, 2156 relationships, 63 execution flows). Use the GitNexus MCP tools to understand code, assess impact, and navigate safely.
+This project is indexed by GitNexus as **taey-ed** (992 symbols, 2593 relationships, 76 execution flows). Use the GitNexus MCP tools to understand code, assess impact, and navigate safely.

 > If any GitNexus tool warns the index is stale, run `npx gitnexus analyze` in terminal first.

@@ -98,4 +98,4 @@ To check whether embeddings exist, inspect `.gitnexus/meta.json` - the `stats.
 | Tools, resources, schema reference | `.claude/skills/gitnexus/gitnexus-guide/SKILL.md` |
 | Index, status, clean, wiki CLI commands | `.claude/skills/gitnexus/gitnexus-cli/SKILL.md` |

-<!-- gitnexus:end -->
\ No newline at end of file
+<!-- gitnexus:end -->
diff --git a/CLAUDE.md b/CLAUDE.md
index 0159312..8e6fedc 100644
--- a/CLAUDE.md
+++ b/CLAUDE.md
@@ -293,7 +293,7 @@ Three-register truth on every claim: **Observed** (verified) / **Inferred** (pat
 <!-- gitnexus:start -->
 # GitNexus - Code Intelligence

-This project is indexed by GitNexus as **taey-ed** (808 symbols, 2156 relationships, 63 execution flows). Use the GitNexus MCP tools to understand code, assess impact, and navigate safely.
+This project is indexed by GitNexus as **taey-ed** (992 symbols, 2593 relationships, 76 execution flows). Use the GitNexus MCP tools to understand code, assess impact, and navigate safely.

 > If any GitNexus tool warns the index is stale, run `npx gitnexus analyze` in terminal first.
```

Current main superseding evidence:

```text
AGENTS.md: This project is indexed by GitNexus as **taey-ed** (11211 symbols, 16377 relationships, 300 execution flows).
CLAUDE.md: This project is indexed by GitNexus as **taey-ed** (11211 symbols, 16377 relationships, 300 execution flows).
```

## Item 5 systemd Drift

Service status:

```text
taey-ed-api.service: ActiveState=active, SubState=running, NeedDaemonReload=yes, FragmentPath=/etc/systemd/system/taey-ed-api.service, DropInPaths=/etc/systemd/system/taey-ed-api.service.d/override.conf
taey-ed-worker.service: ActiveState=active, SubState=running, NeedDaemonReload=yes, FragmentPath=/etc/systemd/system/taey-ed-worker.service, DropInPaths=/etc/systemd/system/taey-ed-worker.service.d/override.conf
```

### taey-ed-api.service

```diff
--- LOADED: systemctl cat taey-ed-api.service
+++ ON-DISK: deploy/systemd/taey-ed-api.service
@@ -7,23 +7,20 @@
 Type=simple
 User=mira
 Group=mira
-WorkingDirectory=/home/mira/taey-ed
+WorkingDirectory=/home/user/taey-ed
 Environment=TAEY_ED_USE_WORKER=1
 Environment=TAEY_ED_PRODUCTION=1
 Environment=PYTHONUNBUFFERED=1
-ExecStart=/home/mira/taey-ed/.venv/bin/python3 /home/mira/taey-ed/.venv/bin/uvicorn spark.server:app --host 0.0.0.0 --port 5003 --timeout-keep-alive 300
+ExecStart=/home/user/taey-ed/.venv/bin/python3 /home/user/taey-ed/.venv/bin/uvicorn spark.server:app --host 0.0.0.0 --port 5003
 Restart=on-failure
 RestartSec=5

-StandardOutput=append:/home/mira/taey-ed/logs/api.log
-StandardError=append:/home/mira/taey-ed/logs/api.log
+StandardOutput=append:/home/user/taey-ed/logs/api.log
+StandardError=append:/home/user/taey-ed/logs/api.log

+# Process hygiene
 KillSignal=SIGINT
 TimeoutStopSec=15

 [Install]
 WantedBy=multi-user.target
-
-[Service]
-Environment=TAEY_ED_SECRETS_PATH=/home/mira/palios-taey-secrets.json
-Environment=TAEY_ED_DATA_DIR=/home/mira/taey-ed-data
```

### taey-ed-worker.service

```diff
--- LOADED: systemctl cat taey-ed-worker.service
+++ ON-DISK: deploy/systemd/taey-ed-worker.service
@@ -7,23 +7,21 @@
 Type=simple
 User=mira
 Group=mira
-WorkingDirectory=/home/mira/taey-ed
+WorkingDirectory=/home/user/taey-ed
 Environment=TAEY_ED_USE_WORKER=1
 Environment=PYTHONUNBUFFERED=1
-Environment=HOME=/home/mira
-ExecStart=/home/mira/taey-ed/.venv/bin/python3 -m spark.worker.run
+# Inherit mira's HOME so `claude` CLI finds the Max-subscription OAuth state
+# under ~/.claude/
+Environment=HOME=/home/user
+ExecStart=/home/user/taey-ed/.venv/bin/python3 -m spark.worker.run
 Restart=on-failure
 RestartSec=5

-StandardOutput=append:/home/mira/taey-ed/logs/worker.log
-StandardError=append:/home/mira/taey-ed/logs/worker.log
+StandardOutput=append:/home/user/taey-ed/logs/worker.log
+StandardError=append:/home/user/taey-ed/logs/worker.log

 KillSignal=SIGINT
 TimeoutStopSec=15

 [Install]
 WantedBy=multi-user.target
-
-[Service]
-Environment=TAEY_ED_SECRETS_PATH=/home/mira/palios-taey-secrets.json
-Environment=TAEY_ED_DATA_DIR=/home/mira/taey-ed-data
```

## Item 6 Docs Scan

Commands:

```bash
find . -maxdepth 3 \( -iname 'README*' -o -path './docs/*' \) -type f | sort
rg -n -i 'TAEY_ED_MODE|blind[- ]?settle|old blind|removed api|removed APIs' docs deploy README* 2>/dev/null || true
rg -n '(store_variant_bt|is_non_deterministic|load_learned|get_quirks_for_screen|screen_signatures[.]get_stats|variant_cache[.]get_stats|escalation_state[.](bump|attempt)|\b(get_stats|bump|attempt)[(]|`(get_stats|bump|attempt)`|`(store_variant_bt|is_non_deterministic|load_learned|get_quirks_for_screen)`)' docs deploy README* 2>/dev/null || true
```

Observed:

```text
./deploy/systemd/README.md
./docs/REQUIREMENTS.md
```

Observed result after `1a2ad7d`: both `rg` scans returned no matches.

## Unknowns

- Unknown: exact origin/date of the on-disk systemd edits that set Mira-specific paths, added `--timeout-keep-alive 300`, and added drop-ins.
- Unknown: whether supervisor wants the stale main stashes dropped after reviewing this report; this task explicitly left them intact.
