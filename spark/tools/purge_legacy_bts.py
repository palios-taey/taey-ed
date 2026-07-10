"""Supersede pre-validation variant BTs (p2-store-purge, 2026-07-10).

The variant store accumulated BTs before the R9.10 validated-persist
lifecycle existed (sources: claude_diagnosis, claude_primary_canonical_*).
They are unverified one-offs — one served a frozen 'Show summary' click
onto a 'Next question' screen the day name-migration made it reachable.
Per R10.1 (supersede, never destroy): behavior_tree is stripped from every
entry whose source is not the validated path, the full entry is preserved
in a superseded_bts sidecar next to the store, and the entry keeps its
metadata plus a supersession marker. Recognition data (hash_index,
signatures) is untouched. state_repo receives a demote event per entry so
the DB history records the supersession.

Usage:
    python3 spark/tools/purge_legacy_bts.py --data-dir DIR [--dry-run] [--json]
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

VALIDATED_SOURCES = {"r9_10_validated_success"}


def purge(data_dir: Path, dry_run: bool) -> dict:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    report: dict = {"dry_run": dry_run, "platforms": {}}
    store_dir = data_dir / "variant_bts"
    for store_path in sorted(store_dir.glob("*.json")):
        platform = store_path.stem
        data = json.loads(store_path.read_text(encoding="utf-8"))
        variants = data.get("variants", {})
        superseded: dict = {}
        for name, entry in variants.items():
            if not entry.get("behavior_tree"):
                continue
            if entry.get("source") in VALIDATED_SOURCES:
                continue
            superseded[name] = dict(entry)
            if not dry_run:
                entry["behavior_tree"] = None
                entry["validated"] = False
                entry["superseded_at"] = stamp
                entry["superseded_reason"] = "pre_validation_source_purge"
                entry["superseded_archive"] = f"superseded_bts/{platform}.{stamp}.json"
        report["platforms"][platform] = {
            "entries": len(variants),
            "superseded": sorted(superseded),
        }
        if superseded and not dry_run:
            sidecar_dir = store_dir / "superseded_bts"
            sidecar_dir.mkdir(parents=True, exist_ok=True)
            (sidecar_dir / f"{platform}.{stamp}.json").write_text(
                json.dumps({"superseded_at": stamp, "entries": superseded}, indent=2),
                encoding="utf-8",
            )
            tmp = store_path.with_suffix(".tmp")
            tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
            tmp.replace(store_path)
            _mirror_demotes(platform, superseded, stamp)
    return report


def _mirror_demotes(platform: str, superseded: dict, stamp: str) -> None:
    try:
        from spark.state_repo import get_state_repo

        repo = get_state_repo()
        for name in superseded:
            try:
                repo.demote_screen_type(
                    platform=platform,
                    screen_type=name,
                    actor="api",
                    evidence={
                        "source": "tools.purge_legacy_bts",
                        "reason": "pre_validation_source_purge",
                        "archived": f"superseded_bts/{platform}.{stamp}.json",
                    },
                )
            except Exception as exc:  # noqa: BLE001 — per-entry mirror is best-effort
                print(f"state_repo mirror skipped for {name}: {exc}", file=sys.stderr)
    except Exception as exc:  # noqa: BLE001
        print(f"state_repo mirror unavailable: {exc}", file=sys.stderr)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, required=True)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    report = purge(args.data_dir, args.dry_run)
    if args.json:
        print(json.dumps(report, indent=2))
    else:
        for platform, info in report["platforms"].items():
            print(f"{platform}: {len(info['superseded'])} superseded of {info['entries']} entries")
            for name in info["superseded"]:
                print(f"  - {name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
