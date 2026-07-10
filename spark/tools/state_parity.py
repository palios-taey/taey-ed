#!/usr/bin/env python3
"""Compare Phase-A file stores with the shadow state database."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from spark.state_db import state_connection, state_db_path
from spark.tasks.paths import DATA_DIR


def _load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _add(report: dict[str, Any], store: str, path: str, issue: str, file_value=None, db_value=None) -> None:
    report["divergences"].append(
        {
            "store": store,
            "path": path,
            "issue": issue,
            "file": file_value,
            "db": db_value,
        }
    )


def _db_screen_for_key(conn, platform: str, key_kind: str, key_hash: str):
    return conn.execute(
        """
        SELECT sk.screen_id, s.screen_type, s.classification
          FROM screen_keys sk
          JOIN screens s ON s.screen_id = sk.screen_id
         WHERE sk.platform=? AND sk.key_kind=? AND sk.key_hash=?
         ORDER BY sk.created_at ASC
         LIMIT 1
        """,
        (platform, key_kind, key_hash),
    ).fetchone()


def _compare_hash_index(conn, data_dir: Path, report: dict[str, Any]) -> None:
    root = data_dir / "hash_index"
    if not root.exists():
        return
    for path in sorted(root.glob("*.json")):
        platform = path.stem
        data = _load_json(path)
        report["checked"]["hash_index_files"] += 1
        for skel_hash, entry in (data.get("hashes") or {}).items():
            report["checked"]["hash_index_entries"] += 1
            row = _db_screen_for_key(conn, platform, "skeleton", skel_hash)
            if row is None:
                _add(report, "hash_index", f"{path}:{skel_hash}", "missing DB screen_key", entry, None)
                continue
            variant = entry.get("variant")
            if variant and variant != "UNKNOWN" and row["screen_type"] != variant:
                _add(
                    report,
                    "hash_index",
                    f"{path}:{skel_hash}",
                    "screen_type mismatch",
                    variant,
                    row["screen_type"],
                )


def _compare_signatures(conn, data_dir: Path, report: dict[str, Any]) -> None:
    root = data_dir / "signatures"
    if not root.exists():
        return
    for path in sorted(root.glob("*.json")):
        platform = path.stem
        data = _load_json(path)
        report["checked"]["signature_files"] += 1
        for sig_hash, entry in (data.get("screens") or {}).items():
            report["checked"]["signature_entries"] += 1
            row = _db_screen_for_key(conn, platform, "signature", sig_hash)
            if row is None:
                _add(report, "signatures", f"{path}:{sig_hash}", "missing DB signature key", entry, None)
                continue
            screen_type = entry.get("screen_type")
            if screen_type and screen_type != "UNKNOWN" and row["screen_type"] != screen_type:
                _add(
                    report,
                    "signatures",
                    f"{path}:{sig_hash}",
                    "screen_type mismatch",
                    screen_type,
                    row["screen_type"],
                )


def _compare_escalation_state(conn, data_dir: Path, report: dict[str, Any]) -> None:
    root = data_dir / "escalation_state"
    if not root.exists():
        return
    for path in sorted(root.glob("*.json")):
        report["checked"]["escalation_files"] += 1
        stem = path.stem
        if "_" not in stem:
            _add(report, "escalation_state", str(path), "unparseable filename", stem, None)
            continue
        platform, hash_prefix = stem.rsplit("_", 1)
        data = _load_json(path)
        rows = conn.execute(
            """
            SELECT c.attempt_count, c.terminal, sk.key_hash
              FROM coordination c
              JOIN screen_keys sk ON sk.screen_id = c.screen_id
             WHERE sk.platform=? AND sk.key_kind='skeleton' AND sk.key_hash LIKE ?
            """,
            (platform, f"{hash_prefix}%"),
        ).fetchall()
        if not rows:
            _add(report, "escalation_state", str(path), "missing DB coordination row", data, None)
            continue
        if len(rows) > 1:
            _add(report, "escalation_state", str(path), "ambiguous DB coordination rows", data, len(rows))
            continue
        row = rows[0]
        if int(data.get("attempt", 0)) != int(row["attempt_count"]):
            _add(report, "escalation_state", str(path), "attempt mismatch", data.get("attempt", 0), row["attempt_count"])
        if bool(data.get("terminal", False)) != bool(row["terminal"]):
            _add(report, "escalation_state", str(path), "terminal mismatch", data.get("terminal"), row["terminal"])


def _compare_consults(conn, consult_dir: Path, report: dict[str, Any]) -> None:
    if not consult_dir.exists():
        return
    for path in sorted(consult_dir.glob("consult_*")):
        if not path.is_dir():
            continue
        meta_path = path / "metadata.json"
        if not meta_path.exists():
            continue
        data = _load_json(meta_path)
        consult_id = data.get("consultation_id") or path.name
        status = data.get("status", "pending")
        report["checked"]["consult_entries"] += 1
        row = conn.execute(
            "SELECT status,payload_dir FROM consults WHERE consult_id=?",
            (consult_id,),
        ).fetchone()
        if row is None:
            _add(report, "consults", str(meta_path), "missing DB consult row", data, None)
            continue
        comparable = status if status in {"pending", "complete", "worker_failed", "abandoned"} else None
        if comparable and row["status"] != comparable:
            _add(report, "consults", str(meta_path), "status mismatch", comparable, row["status"])


def run(data_dir: Path, db_path: Path, consult_dir: Path) -> dict[str, Any]:
    report: dict[str, Any] = {
        "ok": True,
        "data_dir": str(data_dir),
        "db_path": str(db_path),
        "consult_dir": str(consult_dir),
        "checked": {
            "hash_index_files": 0,
            "hash_index_entries": 0,
            "signature_files": 0,
            "signature_entries": 0,
            "escalation_files": 0,
            "consult_entries": 0,
        },
        "divergences": [],
    }
    if not db_path.exists():
        _add(report, "state_db", str(db_path), "state DB does not exist", None, None)
        report["ok"] = False
        return report
    with state_connection(db_path) as conn:
        _compare_hash_index(conn, data_dir, report)
        _compare_signatures(conn, data_dir, report)
        _compare_escalation_state(conn, data_dir, report)
        _compare_consults(conn, consult_dir, report)
    report["ok"] = not report["divergences"]
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Compare taey-ed file stores with taey_state.db.")
    parser.add_argument("--data-dir", type=Path, default=DATA_DIR)
    parser.add_argument("--state-db", type=Path, default=state_db_path())
    parser.add_argument("--consult-dir", type=Path, default=Path("/tmp/taey-ed-consult"))
    parser.add_argument("--json", action="store_true", help="Emit machine-readable JSON only.")
    args = parser.parse_args(argv)

    report = run(args.data_dir, args.state_db, args.consult_dir)
    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        print(f"state parity: {'OK' if report['ok'] else 'DIVERGED'}")
        print(f"data_dir: {report['data_dir']}")
        print(f"state_db: {report['db_path']}")
        print(f"consult_dir: {report['consult_dir']}")
        print("checked:", json.dumps(report["checked"], sort_keys=True))
        for item in report["divergences"]:
            print(f"- {item['store']}: {item['issue']} at {item['path']}")
            print(f"  file={item['file']!r}")
            print(f"  db={item['db']!r}")
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
