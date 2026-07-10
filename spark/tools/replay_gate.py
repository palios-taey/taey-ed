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
                 accepted BTs) is executor-native per the pinned ccm manifest
                 BEFORE any normalizer runs, then STILL passes
                 validate_worker_bt_response for its EXERCISE screen type.
                 Catches the blanket-floor and type=conditional classes.
  C. STORE     — every stored variant behavior_tree has a validated-path
                 source, and no TRANSITION-master entry carries a BT.
                 Catches the frozen-wrong-button class.
  D. PROBES    — named structural contract probes, one per thesis row,
                 missed-contract row, and git-fence assertion. Each probe has
                 a checked-in red-run register entry, count/hash pins, and
                 residue findings instead of exit-code-only checks.

Exit 0 = green (deployable). Exit 1 = red with per-item findings.

Usage:
    python3 spark/tools/replay_gate.py --corpus DIR --data-dir DIR [--json]
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

VALIDATED_SOURCES = {"r9_10_validated_success"}
DEFAULT_EXPECTED_RED_REGISTER = Path(__file__).with_name("replay_expected_red.jsonl")
DEFAULT_CONTRACT_RED_RUN_REGISTER = Path(__file__).with_name("contract_probe_red_runs.jsonl")


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
            yield f"raw_dumps/{dump.name}", platform, screen, bt
    for resp in sorted((corpus / "consults").glob("consult_*/response.json")):
        body = _load(resp)
        bt = body.get("tree") if isinstance(body.get("tree"), dict) else None
        meta = _load(resp.parent / "metadata.json")
        screen = body.get("screen_type") or meta.get("screen_type_hint") or ""
        platform = meta.get("platform") or "khan_academy"
        if bt and screen:
            yield f"consults/{resp.parent.name}/response.json", platform, screen, bt


def _executor_shape_violations(node, path: str = "tree") -> list[str]:
    violations: list[str] = []
    if isinstance(node, dict):
        node_type = node.get("type")
        action = node.get("action")
        if node_type is None:
            if action is None:
                violations.append(f"{path}: missing type/action")
        elif node_type == "action":
            if not isinstance(action, str) or not action:
                violations.append(f"{path}: type='action' missing action")
        elif node_type not in {"sequence", "fallback"}:
            # Bundle tick_node truth (ccm 2026-07-10 verbatim): ANY type outside
            # {sequence, fallback, action} is "Unknown node type" -> FAILURE on
            # the Mac, regardless of the action key. The earlier action==type
            # allowance encoded a wrong dispatcher belief and passed shapes the
            # executor rejects.
            violations.append(f"{path}: executor-unknown type={node_type!r} (action={action!r})")
        for index, child in enumerate(node.get("children") or []):
            violations.extend(_executor_shape_violations(child, f"{path}.children[{index}]"))
        for key in ("do", "then", "else"):
            if key in node:
                violations.extend(_executor_shape_violations(node.get(key), f"{path}.{key}"))
    elif isinstance(node, list):
        for index, item in enumerate(node):
            violations.extend(_executor_shape_violations(item, f"{path}[{index}]"))
    return violations


def _entry_hash(previous_hash: str, entry: dict) -> str:
    encoded = json.dumps(
        {"previous_entry_hash": previous_hash, "entry": entry},
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def _load_expected_red_register(path: Path, findings: list) -> dict[tuple[str, str], dict]:
    entries: dict[tuple[str, str], dict] = {}
    if not path.exists():
        findings.append(f"REGISTER:{path}: expected-red register missing")
        return entries
    previous = "GENESIS"
    for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError as exc:
            findings.append(f"REGISTER:{path}:{lineno}: invalid JSONL: {exc}")
            continue
        entry = record.get("entry")
        if not isinstance(entry, dict):
            findings.append(f"REGISTER:{path}:{lineno}: entry must be an object")
            continue
        expected_previous = record.get("previous_entry_hash")
        if expected_previous != previous:
            findings.append(
                f"REGISTER:{path}:{lineno}: previous_entry_hash {expected_previous!r} "
                f"does not match chain head {previous!r}"
            )
        observed_hash = _entry_hash(previous, entry)
        if record.get("entry_hash") != observed_hash:
            findings.append(
                f"REGISTER:{path}:{lineno}: entry_hash mismatch "
                f"(observed {observed_hash})"
            )
        artifact_path = entry.get("artifact_path")
        violation_hash_value = entry.get("violation_hash")
        if not artifact_path or not violation_hash_value:
            findings.append(f"REGISTER:{path}:{lineno}: artifact_path and violation_hash are required")
        else:
            key = (artifact_path, violation_hash_value)
            if key in entries:
                findings.append(f"REGISTER:{path}:{lineno}: duplicate expected-red key {key}")
            entries[key] = entry
        previous = record.get("entry_hash") or observed_hash
    return entries


def check_served(
    corpus: Path,
    findings: list,
    audit_dir: Path | None = None,
    expected_red: dict[tuple[str, str], dict] | None = None,
) -> dict:
    from spark.tasks.screen_type_assembler import (
        ScreenTypeAssemblerError,
        validate_worker_bt_response,
    )
    from spark.tasks.screen_type_util import get_master_category
    from spark.tasks.bt_lint import lint_bt, summarize_violations, violation_hash, write_lint_audit
    from spark.worker.bt_generator import _normalize_bt_nodes

    expected_red = expected_red or {}
    checked = skipped = executor_shape_violations = bt_lint_rejections = expected_red_rejections = 0
    for name, platform, screen, bt in _iter_served_bts(corpus):
        if get_master_category(screen) != "EXERCISE":
            skipped += 1
            continue
        lint_result = lint_bt(bt)
        if not lint_result.ok:
            bt_lint_rejections += 1
            executor_shape_violations += len(lint_result.violations)
            digest = violation_hash(lint_result)
            artifact = write_lint_audit(
                result=lint_result,
                tree=bt,
                source="replay_gate",
                context={
                    "corpus_item": name,
                    "platform": platform,
                    "screen": screen,
                    "violation_hash": digest,
                    "expected_red": (name, digest) in expected_red,
                },
                audit_dir=audit_dir,
            )
            if (name, digest) in expected_red:
                expected_red_rejections += 1
                continue
            findings.append(
                f"B:{name}: bt_lint rejected production-served {screen} BT: "
                f"{summarize_violations(lint_result)}; "
                f"violation_hash={digest}; audit={artifact}"
            )
            continue
        parsed = {"screen_type": screen, "tree": copy.deepcopy(bt)}
        _normalize_bt_nodes(parsed["tree"])
        shape_violations = _executor_shape_violations(parsed["tree"])
        if shape_violations:
            executor_shape_violations += len(shape_violations)
            findings.append(
                f"B:{name}: executor-native shape violation after normalizer: "
                f"{', '.join(shape_violations[:5])}"
            )
        try:
            validate_worker_bt_response(
                parsed, platform, screen
            )
            checked += 1
        except ScreenTypeAssemblerError as exc:
            findings.append(f"B:{name}: production-served {screen} BT now rejected: {exc}")
        except Exception as exc:  # noqa: BLE001
            skipped += 1
            findings.append(f"B:{name}: validator raised unexpectedly for {screen}: {exc!r}")
    return {
        "served_checked": checked,
        "served_skipped": skipped,
        "executor_shape_violations": executor_shape_violations,
        "bt_lint_rejections": bt_lint_rejections,
        "expected_red_rejections": expected_red_rejections,
        "unregistered_bt_lint_rejections": bt_lint_rejections - expected_red_rejections,
        "expected_red_register_entries": len(expected_red),
    }


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
    parser.add_argument("--audit-dir", type=Path)
    parser.add_argument("--expected-red-register", type=Path, default=DEFAULT_EXPECTED_RED_REGISTER)
    parser.add_argument("--contract-red-run-register", type=Path, default=DEFAULT_CONTRACT_RED_RUN_REGISTER)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    os.environ.setdefault("TAEY_ED_DATA_DIR", str(args.data_dir))

    findings: list[str] = []
    stats = {}
    expected_red = _load_expected_red_register(args.expected_red_register, findings)
    stats.update(check_trees(args.corpus, args.data_dir, findings))
    stats.update(check_served(args.corpus, findings, args.audit_dir, expected_red))
    stats.update(check_store(args.data_dir, findings))
    from spark.tools.contract_probe_harness import run_contract_probes

    probe_report = run_contract_probes(
        repo_root=Path(__file__).resolve().parents[2],
        corpus=args.corpus,
        data_dir=args.data_dir,
        red_run_register=args.contract_red_run_register,
    )
    stats.update(probe_report["stats"])
    findings.extend(probe_report["findings"])

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
