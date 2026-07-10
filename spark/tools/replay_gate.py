"""Recorded-production replay gate (p2-replay-gate, 2026-07-10).

No engine change deploys without replaying the recorded production corpus
through the server's deterministic serving logic. NOT synthetic tests —
recorded reality: real Mac trees, real served BTs, the real variant store.
Production remains the oracle; this gate replays what production already
proved so a change cannot silently un-prove it (Jesse 2026-07-10: every
change was proven only by the next live run, which then broke on something
else — both of today's incidents fail this gate in seconds).

Checks:
  A. TREES     — every recorded tree skeleton-hashes without error; every
                 hash_index variant name is canonical (no legacy names).
  B. SERVED    — every recorded served BT (raw-dump failed_bt = BTs that
                 passed validation and were executed; consult response.json
                 accepted BTs) STILL passes validate_worker_bt_response for
                 its EXERCISE screen type. Catches the blanket-floor class.
  C. STORE     — every stored variant behavior_tree has a validated-path
                 source, and no TRANSITION-master entry carries a BT.
                 Catches the frozen-wrong-button class.

Exit 0 = green (deployable). Exit 1 = red with per-item findings.

Usage:
    python3 spark/tools/replay_gate.py --corpus DIR --data-dir DIR [--json]
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

VALIDATED_SOURCES = {"r9_10_validated_success"}


def _load(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        return {"_load_error": str(exc)}


def check_trees(corpus: Path, data_dir: Path, findings: list) -> dict:
    from spark.tasks.skeleton import extract_skeleton, skeleton_hash
    from spark.tasks.classify_screen import canonicalize_screen_type

    # Raw dumps REDACT the tree ('<tree redacted for brevity>', server.py
    # _dump_raw_failure_bodies) — real recorded trees live in the consult
    # payload dirs and worker handoffs.
    hashed = 0
    tree_paths = sorted((corpus / "consults").glob("consult_*/tree.json"))
    tree_paths += sorted((corpus / "handoffs").glob("*/tree.json"))
    for tree_path in tree_paths:
        tree = _load(tree_path)
        if not isinstance(tree, dict) or "_load_error" in tree:
            findings.append(f"A:{tree_path.parent.name}: tree.json unreadable")
            continue
        try:
            skeleton_hash(extract_skeleton(tree))
            hashed += 1
        except Exception as exc:  # noqa: BLE001
            findings.append(f"A:{tree_path.parent.name}: skeleton hash raised {exc!r}")

    legacy = indeterminate = 0
    for idx_path in sorted((data_dir / "hash_index").glob("*.json")):
        platform = idx_path.stem
        entries = _load(idx_path).get("hashes", {})
        for skel, entry in entries.items():
            name = entry.get("variant") or ""
            canonical = canonicalize_screen_type(platform, name, None)
            if canonical == name:
                continue
            if canonical == "UNKNOWN":
                # Some registered subtypes only resolve with a live tree
                # (subtype-floor logic); tree=None cannot judge them.
                indeterminate += 1
                continue
            legacy += 1
            findings.append(
                f"A:{platform}:{skel[:12]}: legacy variant alias {name!r} (-> {canonical!r})"
            )
    return {"trees_hashed": hashed, "legacy_names": legacy, "names_indeterminate": indeterminate}


def _iter_served_bts(corpus: Path):
    for dump in sorted((corpus / "raw_dumps").glob("*.json")):
        body = _load(dump)
        lr = body.get("last_result") or {}
        bt = lr.get("failed_bt")
        screen = lr.get("screen") or ""
        platform = body.get("platform") or "khan_academy"
        if isinstance(bt, dict) and screen:
            yield dump.name, platform, screen, bt
    for resp in sorted((corpus / "consults").glob("consult_*/response.json")):
        body = _load(resp)
        bt = body.get("tree") if isinstance(body.get("tree"), dict) else None
        meta = _load(resp.parent / "metadata.json")
        screen = body.get("screen_type") or meta.get("screen_type_hint") or ""
        platform = meta.get("platform") or "khan_academy"
        if bt and screen:
            yield f"{resp.parent.name}/response.json", platform, screen, bt


def check_served(corpus: Path, findings: list) -> dict:
    from spark.tasks.screen_type_assembler import (
        ScreenTypeAssemblerError,
        validate_worker_bt_response,
    )
    from spark.tasks.screen_type_util import get_master_category

    checked = skipped = 0
    for name, platform, screen, bt in _iter_served_bts(corpus):
        if get_master_category(screen) != "EXERCISE":
            skipped += 1
            continue
        try:
            validate_worker_bt_response(
                {"screen_type": screen, "tree": bt}, platform, screen
            )
            checked += 1
        except ScreenTypeAssemblerError as exc:
            findings.append(f"B:{name}: production-served {screen} BT now rejected: {exc}")
        except Exception as exc:  # noqa: BLE001
            skipped += 1
            findings.append(f"B:{name}: validator raised unexpectedly for {screen}: {exc!r}")
    return {"served_checked": checked, "served_skipped": skipped}


def check_store(data_dir: Path, findings: list) -> dict:
    from spark.tasks.screen_type_util import get_master_category

    with_bt = bad_source = transition_bt = 0
    for store_path in sorted((data_dir / "variant_bts").glob("*.json")):
        variants = _load(store_path).get("variants", {})
        for name, entry in variants.items():
            if not entry.get("behavior_tree"):
                continue
            with_bt += 1
            if entry.get("source") not in VALIDATED_SOURCES:
                bad_source += 1
                findings.append(
                    f"C:{store_path.stem}:{name}: stored BT from pre-validation source {entry.get('source')!r}"
                )
            if get_master_category(name) == "TRANSITION":
                transition_bt += 1
                findings.append(
                    f"C:{store_path.stem}:{name}: TRANSITION entry carries a stored BT (deterministic-serve only)"
                )
    return {"stored_bts": with_bt, "bad_source": bad_source, "transition_bts": transition_bt}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--corpus", type=Path, required=True)
    parser.add_argument("--data-dir", type=Path, required=True)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    findings: list[str] = []
    stats = {}
    stats.update(check_trees(args.corpus, args.data_dir, findings))
    stats.update(check_served(args.corpus, findings))
    stats.update(check_store(args.data_dir, findings))

    green = not findings
    report = {"green": green, "stats": stats, "findings": findings}
    if args.json:
        print(json.dumps(report, indent=2))
    else:
        for line in findings:
            print(f"RED  {line}")
        print(f"stats: {stats}")
        print("REPLAY GATE: GREEN — corpus replays clean, store invariants hold"
              if green else f"REPLAY GATE: RED — {len(findings)} finding(s)")
    return 0 if green else 1


if __name__ == "__main__":
    raise SystemExit(main())
