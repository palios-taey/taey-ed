#!/usr/bin/env python3
"""Backfill durable file stores into taey_state.db.

Imports only durable DATA_DIR stores. In-flight /tmp queues are deliberately out
of scope for Phase A.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from spark.state_db import init_state_db, state_connection, state_db_path
from spark.state_repo import StateRepo
from spark.tasks.paths import DATA_DIR

MASTER_CATEGORIES = {"NAVIGATION", "VIDEO", "ARTICLE", "EXERCISE", "TRANSITION"}


def _load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"), default=str)


def _sha(value: Any) -> str:
    return hashlib.sha256(_json(value).encode("utf-8")).hexdigest()


def _source_seen(db_path: Path, source_sha: str) -> bool:
    if not db_path.exists():
        return False
    marker = f'"source_sha":"{source_sha}"'
    with state_connection(db_path) as conn:
        return conn.execute(
            "SELECT 1 FROM events WHERE payload_json LIKE ? LIMIT 1",
            (f"%{marker}%",),
        ).fetchone() is not None


def _real_screen_type(screen_type: str | None) -> str | None:
    value = str(screen_type or "").strip()
    if not value or value == "UNKNOWN":
        return None
    if value in MASTER_CATEGORIES:
        return None
    return value


def _evidence(source: str, path: Path, source_sha: str) -> dict[str, Any]:
    return {"source": source, "path": str(path), "source_sha": source_sha}


def _import_hash_index(repo: StateRepo, data_dir: Path, db_path: Path, dry_run: bool, stats: dict[str, int]) -> dict[str, dict]:
    variants_by_platform = _load_variants(data_dir)
    root = data_dir / "hash_index"
    if not root.exists():
        return variants_by_platform
    for path in sorted(root.glob("*.json")):
        platform = path.stem
        data = _load_json(path)
        for skel_hash, entry in (data.get("hashes") or {}).items():
            stats["hash_index_seen"] += 1
            source_sha = _sha(["hash_index", platform, skel_hash, entry])
            if _source_seen(db_path, source_sha):
                stats["skipped_seen"] += 1
                continue
            screen_type = _real_screen_type(entry.get("variant"))
            if screen_type is None:
                stats["skipped_unresolved_type"] += 1
            if dry_run:
                stats["hash_index_importable"] += 1
            else:
                repo.record_classification_result(
                    platform=platform,
                    key_kind="skeleton",
                    key_hash=skel_hash,
                    screen_type=screen_type or "UNKNOWN",
                    success=screen_type is not None,
                    actor="system",
                    evidence=_evidence("state_import.hash_index", path, source_sha),
                )
                stats["hash_index_imported"] += 1
            variant_entry = variants_by_platform.get(platform, {}).get(entry.get("variant") or "")
            if screen_type and variant_entry and variant_entry.get("behavior_tree"):
                _import_behavior_tree(
                    repo,
                    db_path,
                    dry_run,
                    stats,
                    platform,
                    "skeleton",
                    skel_hash,
                    screen_type,
                    variant_entry["behavior_tree"],
                    path,
                    "state_import.variant_bts_for_hash",
                )
    return variants_by_platform


def _load_variants(data_dir: Path) -> dict[str, dict]:
    root = data_dir / "variant_bts"
    variants: dict[str, dict] = {}
    if not root.exists():
        return variants
    for path in sorted(root.glob("*.json")):
        data = _load_json(path)
        variants[path.stem] = data.get("variants") or {}
    return variants


def _import_behavior_tree(
    repo: StateRepo,
    db_path: Path,
    dry_run: bool,
    stats: dict[str, int],
    platform: str,
    key_kind: str,
    key_hash: str,
    screen_type: str,
    behavior_tree: dict[str, Any],
    path: Path,
    source: str,
) -> None:
    stats["behavior_trees_seen"] += 1
    source_sha = _sha([source, platform, key_kind, key_hash, screen_type, behavior_tree])
    if _source_seen(db_path, source_sha):
        stats["skipped_seen"] += 1
        return
    if dry_run:
        stats["behavior_trees_importable"] += 1
        return
    repo.record_behavior_tree(
        platform=platform,
        key_kind=key_kind,
        key_hash=key_hash,
        bt_json=behavior_tree,
        built_by="system",
        source_kind=source,
        actor="system",
        evidence=_evidence(source, path, source_sha),
        screen_type=screen_type,
        status="candidate",
    )
    stats["behavior_trees_imported"] += 1


def _import_signatures(repo: StateRepo, data_dir: Path, db_path: Path, dry_run: bool, stats: dict[str, int]) -> None:
    root = data_dir / "signatures"
    if not root.exists():
        return
    for path in sorted(root.glob("*.json")):
        platform = path.stem
        data = _load_json(path)
        for sig_hash, entry in (data.get("screens") or {}).items():
            stats["signatures_seen"] += 1
            source_sha = _sha(["signatures", platform, sig_hash, entry])
            if _source_seen(db_path, source_sha):
                stats["skipped_seen"] += 1
                continue
            screen_type = _real_screen_type(entry.get("screen_type"))
            if screen_type is None:
                stats["skipped_unresolved_type"] += 1
            if dry_run:
                stats["signatures_importable"] += 1
            else:
                repo.record_classification_result(
                    platform=platform,
                    key_kind="signature",
                    key_hash=sig_hash,
                    screen_type=screen_type or "UNKNOWN",
                    success=screen_type is not None,
                    features={"signature": entry.get("signature") or [], "extract": entry.get("extract")},
                    actor="system",
                    evidence=_evidence("state_import.signatures", path, source_sha),
                )
                stats["signatures_imported"] += 1
            if screen_type and entry.get("behavior_tree"):
                _import_behavior_tree(
                    repo,
                    db_path,
                    dry_run,
                    stats,
                    platform,
                    "signature",
                    sig_hash,
                    screen_type,
                    entry["behavior_tree"],
                    path,
                    "state_import.signature_bt",
                )


def _import_session_archives(repo: StateRepo, data_dir: Path, db_path: Path, dry_run: bool, stats: dict[str, int]) -> None:
    root = data_dir / "screen_sessions"
    if not root.exists():
        return
    for archive_dir in sorted(root.glob("*/archive")):
        platform = archive_dir.parent.name
        for path in sorted(archive_dir.glob("*.jsonl")):
            skel_hash = path.stem
            for line_no, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
                if not line.strip():
                    continue
                stats["session_archive_seen"] += 1
                payload = json.loads(line)
                source_sha = _sha(["screen_session_archive", platform, skel_hash, line_no, payload])
                if _source_seen(db_path, source_sha):
                    stats["skipped_seen"] += 1
                    continue
                if dry_run:
                    stats["session_archive_importable"] += 1
                    continue
                inserted = repo.import_session_archive_entry(
                    platform=platform,
                    skel_hash=skel_hash,
                    source_sha=source_sha,
                    actor="system",
                    evidence=_evidence("state_import.screen_session_archive", path, source_sha),
                    payload={"line_no": line_no, "archive": payload},
                )
                stats["session_archive_imported"] += 1 if inserted else 0


def _import_escalation_state(repo: StateRepo, data_dir: Path, db_path: Path, dry_run: bool, stats: dict[str, int]) -> None:
    root = data_dir / "escalation_state"
    if not root.exists():
        return
    for path in sorted(root.glob("*.json")):
        stats["escalation_seen"] += 1
        stem = path.stem
        if "_" not in stem:
            raise ValueError(f"cannot parse escalation_state filename: {path}")
        platform, hash_prefix = stem.rsplit("_", 1)
        payload = _load_json(path)
        source_sha = _sha(["escalation_state", platform, hash_prefix, payload])
        if _source_seen(db_path, source_sha):
            stats["skipped_seen"] += 1
            continue
        if dry_run:
            stats["escalation_importable"] += 1
            continue
        repo.import_escalation_snapshot(
            platform=platform,
            screen_hash=hash_prefix,
            attempt_count=int(payload.get("attempt", 0) or 0),
            terminal=bool(payload.get("terminal", False)),
            actor="system",
            evidence=_evidence("state_import.escalation_state", path, source_sha),
        )
        stats["escalation_imported"] += 1


def _empty_stats() -> dict[str, int]:
    keys = (
        "hash_index_seen",
        "hash_index_importable",
        "hash_index_imported",
        "signatures_seen",
        "signatures_importable",
        "signatures_imported",
        "behavior_trees_seen",
        "behavior_trees_importable",
        "behavior_trees_imported",
        "session_archive_seen",
        "session_archive_importable",
        "session_archive_imported",
        "escalation_seen",
        "escalation_importable",
        "escalation_imported",
        "skipped_seen",
        "skipped_unresolved_type",
    )
    return {key: 0 for key in keys}


def run(data_dir: Path, db_path: Path, dry_run: bool) -> dict[str, Any]:
    stats = _empty_stats()
    repo = StateRepo(db_path=db_path)
    if not dry_run:
        init_state_db(db_path)
    variants = _import_hash_index(repo, data_dir, db_path, dry_run, stats)
    stats["variant_files_seen"] = len(variants)
    _import_signatures(repo, data_dir, db_path, dry_run, stats)
    _import_session_archives(repo, data_dir, db_path, dry_run, stats)
    _import_escalation_state(repo, data_dir, db_path, dry_run, stats)
    result: dict[str, Any] = {
        "ok": True,
        "dry_run": dry_run,
        "data_dir": str(data_dir),
        "db_path": str(db_path),
        "stats": stats,
    }
    if db_path.exists():
        result["db_counts"] = StateRepo(db_path=db_path).counts()
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Backfill durable taey-ed file stores into taey_state.db.")
    parser.add_argument("--data-dir", type=Path, default=DATA_DIR)
    parser.add_argument("--state-db", type=Path, default=state_db_path())
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    try:
        result = run(args.data_dir, args.state_db, args.dry_run)
    except Exception as exc:
        if args.json:
            print(json.dumps({"ok": False, "error": str(exc)}, indent=2, sort_keys=True))
        else:
            print(f"state import failed: {exc}", file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps(result, indent=2, sort_keys=True))
    else:
        print(f"state import: {'DRY RUN' if result['dry_run'] else 'APPLIED'}")
        print(f"data_dir: {result['data_dir']}")
        print(f"state_db: {result['db_path']}")
        print("stats:", json.dumps(result["stats"], sort_keys=True))
        if "db_counts" in result:
            print("db_counts:", json.dumps(result["db_counts"], sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
