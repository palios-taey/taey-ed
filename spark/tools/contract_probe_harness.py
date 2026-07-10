"""Contract probes for the replay deploy gate.

These are not synthetic unit tests. Each probe names a production incident or
canon row and is paired with an append-only red-run register entry. The gate
inspects residue directly: missing evidence, relaxed source guards, or a corpus
fixture that no longer fails closed becomes a replay-gate finding.
"""

from __future__ import annotations

import ast
import contextlib
import hashlib
import io
import json
import os
import subprocess
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Callable


PINNED_EXECUTOR_MANIFEST_HASH = (
    "sha256:3fdf2102343c213dd7e0d05d002054dd953ae8468e164df4769bea6180e7f26f"
)
PINNED_EXECUTOR_MANIFEST_V2_SHA256 = (
    "3ffc6274ab27fa41ce51d036944a5e02ce07f36ab071265429e30c1675410d34"
)
PINNED_PROBE_MIN_COUNT = 23
PINNED_PROBE_REGISTRY_HASH = "sha256:cd5f07d473cc8655574b93682afbdb20c1ca2666145d044b02aba51636d78c53"
DEFAULT_CONTRACT_RED_RUN_REGISTER = Path(__file__).with_name("contract_probe_red_runs.jsonl")

# V2 names the select_dropdown_option surface as an 11-slot minimum; the live
# handler also accepts compatibility aliases, so the probe enforces >= 11 and
# checks every V2 alias observed in the document.
V2_SELECT_DROPDOWN_PARAM_SURFACE = (
    "option",
    "option_text",
    "target",
    "trigger_element",
    "element",
    "trigger_target",
    "combobox",
    "trigger_role",
    "trigger_match_mode",
    "open_strategy",
    "open_wait",
    "verify_wait",
    "strategies",
)


@dataclass(frozen=True)
class ProbeSpec:
    id: str
    group: str
    check_name: str
    current_mode: str
    incident_ref: str
    red_fixture: str
    description: str


@dataclass(frozen=True)
class ProbeContext:
    repo_root: Path
    corpus: Path
    data_dir: Path


PROBE_SPECS: tuple[ProbeSpec, ...] = (
    ProbeSpec(
        id="thesis.executor_node_type_vocab",
        group="structural-gap-thesis",
        check_name="manifest_conditional_red_fixture",
        current_mode="enforced",
        incident_ref="manifest expected-red consult_1783693653_4d83af4a",
        red_fixture="consults/consult_1783693653_4d83af4a/response.json",
        description="Mac executor only accepts sequence/fallback/action node types.",
    ),
    ProbeSpec(
        id="thesis.composable_action_shape",
        group="structural-gap-thesis",
        check_name="manifest_composable_contract",
        current_mode="enforced",
        incident_ref="manifest expected-red consult_1783696442_0b0f4cd5",
        red_fixture="consults/consult_1783696442_0b0f4cd5/response.json",
        description="for_each/conditional are action nodes with node-level structural keys.",
    ),
    ProbeSpec(
        id="thesis.worker_bt_dialect_boundary",
        group="structural-gap-thesis",
        check_name="served_lint_before_normalizer",
        current_mode="enforced",
        incident_ref="raw served BTs passed worker validation but failed executor manifest",
        red_fixture="raw_dumps/raw_20260710_011658.json",
        description="Recorded served BTs are linted before any server normalizer can repair them.",
    ),
    ProbeSpec(
        id="thesis.expected_next_seed_never_veto",
        group="structural-gap-thesis",
        check_name="expected_next_seed_never_veto",
        current_mode="enforced",
        incident_ref="e949aaa persist gating",
        red_fixture="git:e949aaa",
        description="Worker/directive expected_next guesses seed learning, never veto first observed advance.",
    ),
    ProbeSpec(
        id="thesis.validated_store_source",
        group="structural-gap-thesis",
        check_name="validated_store_source",
        current_mode="enforced",
        incident_ref="p2 replay loop validate-store-replay",
        red_fixture="data_dir:variant_bts pre-validation source scan",
        description="Only R9.10 validated-success writes can persist reusable BTs.",
    ),
    ProbeSpec(
        id="thesis.worker_handoff_receipt",
        group="structural-gap-thesis",
        check_name="worker_handoff_receipt",
        current_mode="enforced",
        incident_ref="worker handoff input contract row",
        red_fixture="handoffs/consult_1783696442_0b0f4cd5_p7qad__p",
        description="Worker handoff carries tree, screenshot, artifact metadata, and KB receipt fields.",
    ),
    ProbeSpec(
        id="thesis.worker_output_schema_retry",
        group="structural-gap-thesis",
        check_name="worker_output_schema_retry",
        current_mode="enforced",
        incident_ref="cl1-worker-schema jsonschema + bounded retry contract",
        red_fixture="worker output missing slots/evidence/confidence",
        description="Worker output is server-side JSON-schema validated, retried once with violation feedback, then escalated with rejected output.",
    ),
    ProbeSpec(
        id="thesis.engine_primitives",
        group="structural-gap-thesis",
        check_name="engine_primitives",
        current_mode="enforced",
        incident_ref="cl2-primitives wait-until-stable + scoped addressing",
        red_fixture="successive posted AX-tree snapshots only",
        description="Engine owns bounded posted-tree wait-until-stable and scoped exact addressing without app telemetry dependency.",
    ),
    ProbeSpec(
        id="thesis.ladder_timer_monotone_bound",
        group="structural-gap-thesis",
        check_name="state_ladder_liveness",
        current_mode="enforced",
        incident_ref="b67d327 state: stop timer ladder climbs",
        red_fixture="spark/tools/state_closure_suite.py --liveness",
        description="Timer resume never increments the escalation attempt count.",
    ),
    ProbeSpec(
        id="thesis.yaml_fold_reset",
        group="structural-gap-thesis",
        check_name="yaml_fold_reset",
        current_mode="enforced",
        incident_ref="b67d327 yaml-fold liveness",
        red_fixture="spark/tools/state_closure_suite.py --liveness yaml_fold",
        description="A non-terminal YAML fold clears stale ladder residue; terminal remains sticky.",
    ),
    ProbeSpec(
        id="thesis.escalation_auto_dispatch_funnel",
        group="structural-gap-thesis",
        check_name="escalation_auto_dispatch",
        current_mode="enforced",
        incident_ref="auto-dispatch missed emission site",
        red_fixture="spark/routes/next_action.py:_escalate_to_claude_diagnosing",
        description="Every non-terminal escalation builds one packet and dispatches Tier 2/3 once.",
    ),
    ProbeSpec(
        id="thesis.canonical_variant_lookup",
        group="structural-gap-thesis",
        check_name="canonical_variant_lookup",
        current_mode="enforced",
        incident_ref="canonical-name lookup replay loop",
        red_fixture="hash_index legacy alias scan",
        description="Hash and BT lookups canonicalize legacy names before serving.",
    ),
    ProbeSpec(
        id="thesis.poison_frozen_bt_store_guard",
        group="structural-gap-thesis",
        check_name="store_poison_guards",
        current_mode="enforced",
        incident_ref="frozen-wrong-button and transition-store defects",
        red_fixture="data_dir:variant_bts store scan",
        description="Variant cache refuses UNKNOWN, bare masters, transitions, and frozen-answer exercise BTs.",
    ),
    ProbeSpec(
        id="thesis.rejection_capture_artifacts",
        group="structural-gap-thesis",
        check_name="rejection_capture_artifacts",
        current_mode="enforced",
        incident_ref="p2 rejection-capture",
        red_fixture="consults/consult_conformance_smoke_1989393_3/rejected_bt.json",
        description="Worker rejection artifacts are persisted and copied into escalation packets.",
    ),
    ProbeSpec(
        id="missed.billing_completion_debit",
        group="codex-missed-contract",
        check_name="billing_contract_registered",
        current_mode="registered_red",
        incident_ref="sg report missed billing contract",
        red_fixture="spark/storage/credits.py:debit_screen no production callsite",
        description="Billing has an idempotent debit primitive; completion wiring remains registered expected-red.",
    ),
    ProbeSpec(
        id="missed.unknown_never_worker",
        group="codex-missed-contract",
        check_name="unknown_never_worker",
        current_mode="enforced",
        incident_ref="557bfa8 UNKNOWN guard plus Step 5D resurrection",
        red_fixture="spark/routes/next_action.py Step 5D UNKNOWN branch",
        description="UNKNOWN classifications fail closed to operator escalation and never create worker consultations.",
    ),
    ProbeSpec(
        id="missed.handler_param_manifest_v2",
        group="codex-missed-contract",
        check_name="handler_param_manifest_v2",
        current_mode="enforced",
        incident_ref="2026-07-10 executor manifest V2",
        red_fixture="dispatches/2026-07-10_executor_manifest_v2.md",
        description="V2 handler value domains are pinned; handler blackboard access is mediated by _tick_action.",
    ),
    ProbeSpec(
        id="missed.paths_fail_loud_env",
        group="codex-missed-contract",
        check_name="paths_fail_loud_env",
        current_mode="enforced",
        incident_ref="supervisor audit 2026-07-10 env-free tools must not touch implicit data roots",
        red_fixture="env -u TAEY_ED_DATA_DIR python -c 'import spark.tasks.paths'",
        description="Path resolution fails loudly without TAEY_ED_DATA_DIR instead of silently using an operator-local default.",
    ),
    ProbeSpec(
        id="missed.recipe_ast_slot_schema",
        group="codex-missed-contract",
        check_name="recipe_ast_registered",
        current_mode="registered_red",
        incident_ref="sg report recipe AST migration gap",
        red_fixture="screen_types/*.yaml prose recipe surface",
        description="Recipe AST and slot schemas are not yet compiler-ready; coverage is registered expected-red.",
    ),
    ProbeSpec(
        id="git_fence.per_type_solve_mode",
        group="git-fence",
        check_name="per_type_solve_mode",
        current_mode="enforced",
        incident_ref="928540d",
        red_fixture="git:928540d",
        description="send_to_llm is required only when that subtype recipe prescribes it.",
    ),
    ProbeSpec(
        id="git_fence.dropdown_opts_gate_fresh_ref",
        group="git-fence",
        check_name="dropdown_opts_gate_fresh_ref",
        current_mode="enforced",
        incident_ref="2baaf53/bf3ad1d",
        red_fixture="spark/platforms/khan_academy/screen_types/EXERCISE_DROPDOWN.yaml",
        description="Dropdown recipe gates non-empty options and re-finds fresh trigger refs before select.",
    ),
    ProbeSpec(
        id="git_fence.no_revert_resurrection",
        group="git-fence",
        check_name="no_revert_resurrection",
        current_mode="enforced",
        incident_ref="c9cbdba/0c30353",
        red_fixture="spark/tasks/screen_type_assembler.py staging cycle history",
        description="Sorter staging allowance remains scoped, not a blanket revert resurrection.",
    ),
    ProbeSpec(
        id="git_fence.seed_never_veto",
        group="git-fence",
        check_name="expected_next_seed_never_veto",
        current_mode="enforced",
        incident_ref="e949aaa",
        red_fixture="git:e949aaa",
        description="The e949aaa expected_next seed-never-veto behavior remains present.",
    ),
)


def registry_hash() -> str:
    payload = [asdict(spec) for spec in PROBE_SPECS]
    encoded = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def run_contract_probes(
    *,
    repo_root: Path,
    corpus: Path,
    data_dir: Path,
    red_run_register: Path | None = None,
) -> dict:
    findings: list[str] = []
    register_path = red_run_register or DEFAULT_CONTRACT_RED_RUN_REGISTER
    red_runs = _load_red_run_register(register_path, findings)

    observed_registry_hash = registry_hash()
    if observed_registry_hash != PINNED_PROBE_REGISTRY_HASH:
        findings.append(
            "PROBE:registry: hash drift "
            f"(observed {observed_registry_hash}, pinned {PINNED_PROBE_REGISTRY_HASH})"
        )
    if len(PROBE_SPECS) < PINNED_PROBE_MIN_COUNT:
        findings.append(
            f"PROBE:registry: probe count decreased to {len(PROBE_SPECS)} "
            f"(minimum {PINNED_PROBE_MIN_COUNT})"
        )

    try:
        from spark.tasks.executor_manifest import EXECUTOR_MANIFEST

        manifest_hash = str(EXECUTOR_MANIFEST["bundle_hash"])
    except Exception as exc:  # noqa: BLE001
        manifest_hash = ""
        findings.append(f"PROBE:manifest: import failed: {exc!r}")
    if manifest_hash != PINNED_EXECUTOR_MANIFEST_HASH:
        findings.append(
            "PROBE:manifest: executor manifest drift "
            f"(observed {manifest_hash!r}, pinned {PINNED_EXECUTOR_MANIFEST_HASH})"
        )

    missing_red = [spec.id for spec in PROBE_SPECS if spec.id not in red_runs]
    if missing_red:
        findings.append(
            "PROBE:red-register: missing checked-in red run(s): "
            + ", ".join(sorted(missing_red))
        )

    ctx = ProbeContext(repo_root=repo_root, corpus=corpus, data_dir=data_dir)
    results = []
    for spec in PROBE_SPECS:
        check = CHECKS.get(spec.check_name)
        if check is None:
            residues = [f"unknown check_name {spec.check_name!r}"]
        else:
            try:
                residues = check(ctx)
            except Exception as exc:  # noqa: BLE001
                residues = [f"probe raised {exc!r}"]
        if residues:
            findings.extend(f"PROBE:{spec.id}: {residue}" for residue in residues)
        results.append(
            {
                "id": spec.id,
                "group": spec.group,
                "mode": spec.current_mode,
                "ok": not residues,
                "residue_count": len(residues),
            }
        )

    failures = sum(1 for item in results if not item["ok"])
    stats = {
        "contract_probe_count": len(PROBE_SPECS),
        "contract_probe_failures": failures,
        "contract_probe_enforced": sum(1 for spec in PROBE_SPECS if spec.current_mode == "enforced"),
        "contract_probe_registered_red": sum(
            1 for spec in PROBE_SPECS if spec.current_mode == "registered_red"
        ),
        "contract_probe_red_run_entries": len(red_runs),
        "contract_probe_registry_hash": observed_registry_hash,
        "contract_probe_manifest_hash": manifest_hash,
        "contract_probe_manifest_v2_sha256": PINNED_EXECUTOR_MANIFEST_V2_SHA256,
    }
    return {"stats": stats, "findings": findings, "results": results}


def _load_json(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        return {"_load_error": str(exc)}


def _source(ctx: ProbeContext, rel_path: str) -> str:
    return (ctx.repo_root / rel_path).read_text(encoding="utf-8")


def _require_source(ctx: ProbeContext, rel_path: str, snippets: tuple[str, ...]) -> list[str]:
    text = _source(ctx, rel_path)
    return [
        f"{rel_path} missing snippet {snippet!r}"
        for snippet in snippets
        if snippet not in text
    ]


def _forbid_source(ctx: ProbeContext, rel_path: str, snippets: tuple[str, ...]) -> list[str]:
    text = _source(ctx, rel_path)
    return [
        f"{rel_path} contains forbidden snippet {snippet!r}"
        for snippet in snippets
        if snippet in text
    ]


def _fixture_tree(ctx: ProbeContext, rel_path: str):
    body = _load_json(ctx.corpus / rel_path)
    if isinstance(body.get("tree"), dict):
        return body["tree"]
    failed_bt = (body.get("last_result") or {}).get("failed_bt")
    if isinstance(failed_bt, dict):
        return failed_bt
    return None


def _lint_fixture_rules(ctx: ProbeContext, rel_path: str, required_rules: set[str]) -> list[str]:
    from spark.tasks.bt_lint import lint_bt

    tree = _fixture_tree(ctx, rel_path)
    if not isinstance(tree, dict):
        return [f"{rel_path} does not contain a fixture BT"]
    result = lint_bt(tree)
    observed = {violation.rule for violation in result.violations}
    missing = sorted(required_rules - observed)
    residues = []
    if result.ok:
        residues.append(f"{rel_path} unexpectedly linted green")
    if missing:
        residues.append(f"{rel_path} missing expected lint rules {missing}")
    return residues


def _entry_hash(previous_hash: str, entry: dict) -> str:
    encoded = json.dumps(
        {"previous_entry_hash": previous_hash, "entry": entry},
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def _load_red_run_register(path: Path, findings: list[str]) -> dict[str, dict]:
    entries: dict[str, dict] = {}
    if not path.exists():
        findings.append(f"PROBE:red-register:{path}: missing")
        return entries
    previous = "GENESIS"
    for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError as exc:
            findings.append(f"PROBE:red-register:{path}:{lineno}: invalid JSONL: {exc}")
            continue
        entry = record.get("entry")
        if not isinstance(entry, dict):
            findings.append(f"PROBE:red-register:{path}:{lineno}: entry must be an object")
            continue
        expected_previous = record.get("previous_entry_hash")
        if expected_previous != previous:
            findings.append(
                f"PROBE:red-register:{path}:{lineno}: previous_entry_hash "
                f"{expected_previous!r} does not match {previous!r}"
            )
        observed_hash = _entry_hash(previous, entry)
        if record.get("entry_hash") != observed_hash:
            findings.append(
                f"PROBE:red-register:{path}:{lineno}: entry_hash mismatch "
                f"(observed {observed_hash})"
            )
        probe_id = entry.get("probe_id")
        if not probe_id:
            findings.append(f"PROBE:red-register:{path}:{lineno}: probe_id required")
        elif probe_id in entries:
            findings.append(f"PROBE:red-register:{path}:{lineno}: duplicate probe_id {probe_id}")
        else:
            if not entry.get("red_fixture") or not entry.get("red_observation"):
                findings.append(
                    f"PROBE:red-register:{path}:{lineno}: red_fixture and red_observation required"
                )
            entries[probe_id] = entry
        previous = record.get("entry_hash") or observed_hash
    return entries


def _check_manifest_conditional_red_fixture(ctx: ProbeContext) -> list[str]:
    return _lint_fixture_rules(
        ctx,
        "consults/consult_1783693653_4d83af4a/response.json",
        {"M8.1", "M8.2"},
    )


def _check_manifest_composable_contract(ctx: ProbeContext) -> list[str]:
    residues = _lint_fixture_rules(
        ctx,
        "consults/consult_1783696442_0b0f4cd5/response.json",
        {"M8.1", "M8.2"},
    )
    residues.extend(
        _require_source(
            ctx,
            "spark/tasks/bt_lint.py",
            (
                "for_each.do must be one node object",
                "conditional.{key} must be one node object when present",
                "COMPOSABLE_ACTION_SET",
                "if action in COMPOSABLE_ACTION_SET:",
            ),
        )
    )
    return residues


def _check_served_lint_before_normalizer(ctx: ProbeContext) -> list[str]:
    residues = _lint_fixture_rules(
        ctx,
        "raw_dumps/raw_20260710_011658.json",
        {"M8.3"},
    )
    text = _source(ctx, "spark/tools/replay_gate.py")
    lint_at = text.find("lint_result = lint_bt(bt)")
    normalize_at = text.find("_normalize_bt_nodes(parsed[\"tree\"])")
    if lint_at < 0:
        residues.append("replay_gate no longer lints served BTs")
    if normalize_at < 0:
        residues.append("replay_gate no longer validates the normalized legacy path")
    if lint_at >= 0 and normalize_at >= 0 and lint_at > normalize_at:
        residues.append("replay_gate normalizes before the manifest lint")
    if "expected_red_rejections" not in text:
        residues.append("replay_gate lost expected-red residue accounting")
    return residues


def _check_expected_next_seed_never_veto(ctx: ProbeContext) -> list[str]:
    return _require_source(
        ctx,
        "spark/routes/next_action.py",
        (
            "gating_is_learned = bool(learned_next)",
            "A wrong worker guess is not evidence against the advance",
            "expected_match = None",
            "observed_advance = (",
            "expected_next = [vr[\"new_screen\"]]",
            "source=\"r9_10_validated_success\"",
        ),
    )


def _check_validated_store_source(ctx: ProbeContext) -> list[str]:
    residues = _require_source(
        ctx,
        "spark/routes/next_action.py",
        (
            "source=\"r9_10_validated_success\"",
            "source_kind=\"r9_10_validated_success\"",
            "\"source\": \"next_action.r9_10_validated_success\"",
        ),
    )
    residues.extend(
        _require_source(
            ctx,
            "spark/tools/replay_gate.py",
            (
                "VALIDATED_SOURCES = {\"r9_10_validated_success\"}",
                "stored BT from pre-validation source",
                "TRANSITION entry carries a stored BT",
            ),
        )
    )
    return residues


def _check_worker_handoff_receipt(ctx: ProbeContext) -> list[str]:
    residues = _require_source(
        ctx,
        "spark/tasks/screen_type_assembler.py",
        (
            "tree_path.write_text",
            "shutil.copy2(screenshot_path, screenshot_target)",
            "\"artifact_path\": str(artifact[\"path\"])",
            "\"artifact_kind\": artifact[\"kind\"]",
            "\"kb_chunks_included\": kb_block.count(\"--- chunk \")",
        ),
    )
    handoffs = list((ctx.corpus / "handoffs").glob("consult_1783696442_0b0f4cd5_*/tree.json"))
    if not handoffs:
        residues.append("recorded handoff fixture consult_1783696442_0b0f4cd5_* missing")
    return residues


def _check_worker_output_schema_retry(ctx: ProbeContext) -> list[str]:
    residues = _require_source(
        ctx,
        "spark/worker/bt_generator.py",
        (
            "from jsonschema import Draft202012Validator",
            "WORKER_SCHEMA_MAX_ATTEMPTS = 2",
            "WORKER_OUTPUT_SCHEMA",
            "\"slots\"",
            "\"evidence\"",
            "\"confidence\"",
            "SERVER-SIDE WORKER OUTPUT REJECTION",
            "user_instruction_retry.txt",
            "worker_rejection_attempt{attempt}.json",
            "worker_output_schema_rejection",
            "rejected_bt.json",
            "new dialect repairs",
        ),
    )
    residues.extend(
        _require_source(
            ctx,
            "spark/routes/next_action.py",
            (
                "worker_output_schema_rejection",
                "failed to load rejected_bt.json for worker rejection escalation",
            ),
        )
    )
    residues.extend(
        _require_source(
            ctx,
            "requirements.txt",
            ("jsonschema",),
        )
    )
    return residues


def _check_engine_primitives(ctx: ProbeContext) -> list[str]:
    residues = _require_source(
        ctx,
        "spark/tasks/deterministic_resolvers.py",
        (
            "STABILITY_REQUIRED_MATCHES = 2",
            "STABILITY_MAX_POLLS = 6",
            "tree_stability_digest",
            "observe_wait_until_stable",
            "build_wait_until_stable_directive",
            "\"engine_wait_until_stable\"",
            "resolve_scoped_address",
            "build_scoped_click_bt",
            "match_mode=\"exact\"",
        ),
    )
    residues.extend(
        _forbid_source(
            ctx,
            "app/tasks/bt_core.py",
            (
                "def wire_snapshot",
                "def find_all_results",
                "def mark_find_all",
                "if action_name == \"find_all\":",
                "\"bt_blackboard\": ctx.blackboard.wire_snapshot()",
                "\"bt_find_all_results\": ctx.blackboard.find_all_results()",
            ),
        )
    )
    residues.extend(
        _forbid_source(
            ctx,
            "app/pipeline.py",
            (
                "last_result[\"bt_blackboard\"] = bt_result[\"bt_blackboard\"]",
                "last_result[\"bt_find_all_results\"] = bt_result[\"bt_find_all_results\"]",
            ),
        )
    )
    residues.extend(
        _forbid_source(
            ctx,
            "spark/models.py",
            (
                "bt_blackboard: Optional[dict] = None",
                "bt_find_all_results: Optional[dict] = None",
            ),
        )
    )
    residues.extend(
        _forbid_source(
            ctx,
            "spark/routes/next_action.py",
            (
                "bt telemetry: blackboard_keys=%s find_all_keys=%s",
                "lr.bt_blackboard",
                "lr.bt_find_all_results",
            ),
        )
    )
    residues.extend(
        _forbid_source(
            ctx,
            "spark/tasks/deterministic_resolvers.py",
            (
                "find_all_stability_digest",
                "last_result_signal_digest",
                "bt_find_all_results",
            ),
        )
    )
    return residues


def _state_liveness_residue() -> list[str]:
    from spark.tools import state_closure_suite

    output = io.StringIO()
    with contextlib.redirect_stdout(output):
        bad = state_closure_suite.app_liveness()
    return [str(item) for item in bad]


def _check_state_ladder_liveness(ctx: ProbeContext) -> list[str]:
    residues = _state_liveness_residue()
    residues.extend(
        _require_source(
            ctx,
            "spark/state_repo.py",
            (
                "if not attempt_key:",
                "if row[\"last_attempt_key\"] == attempt_key:",
                "resume_diagnosis_cycle",
                "UPDATE coordination",
            ),
        )
    )
    return residues


def _check_yaml_fold_reset(ctx: ProbeContext) -> list[str]:
    residues = _state_liveness_residue()
    residues.extend(
        _require_source(
            ctx,
            "spark/routes/next_action.py",
            (
                "yaml_fold_resets_ladder",
                "YAML edited after last attempt",
                "escalation_state.clear(platform, _screen_hash",
            ),
        )
    )
    residues.extend(
        _require_source(
            ctx,
            "spark/state_repo.py",
            (
                "cleared_reason == \"yaml_fold\" and int(old[\"terminal\"]) == 1",
                "SET state='terminal'",
                "yaml_sha_at_attempt=NULL",
            ),
        )
    )
    return residues


def _check_escalation_auto_dispatch(ctx: ProbeContext) -> list[str]:
    residues = _require_source(
        ctx,
        "spark/routes/next_action.py",
        (
            "build_packet(",
            "dispatch_body_for_tier",
            "dispatch_tier_once",
            "notify_fleet(\"taeys-hands\"",
        ),
    )
    residues.extend(
        _require_source(
            ctx,
            "spark/tasks/escalation.py",
            (
                "def build_packet(",
                "def notify_fleet(",
                "def dispatch_body_for_tier(",
                "packet_path: Path",
            ),
        )
    )
    return residues


def _check_canonical_variant_lookup(ctx: ProbeContext) -> list[str]:
    from spark.tasks.classify_screen import canonicalize_screen_type

    residues = []
    pairs = {
        "VIDEO_PLAYING": "VIDEO__PLAYER",
        "TRANSITION_EXERCISE_COMPLETE": "TRANSITION__SUMMARY",
        "EXERCISE_CHECKBOX": "EXERCISE_MULTIPLE_SELECT",
    }
    for source, expected in pairs.items():
        observed = canonicalize_screen_type("khan_academy", source, None)
        if observed != expected:
            residues.append(f"canonicalize_screen_type({source!r}) -> {observed!r}, expected {expected!r}")
    residues.extend(
        _require_source(
            ctx,
            "spark/tasks/variant_cache.py",
            (
                "variant = _canonical_variant(platform, variant)",
                "\"variant\": _canonical_variant(platform, entry[\"variant\"])",
                "_canonical_expected_next(platform",
            ),
        )
    )
    return residues


def _check_store_poison_guards(ctx: ProbeContext) -> list[str]:
    from spark.tasks.variant_cache import _cache_safe_behavior_tree

    residues = []
    frozen = {
        "type": "action",
        "action": "find_and_click",
        "params": {"role": "AXRadioButton", "target": "Choice A"},
    }
    generic = {
        "type": "action",
        "action": "send_to_llm",
        "params": {"question": "q", "question_type": "solve"},
    }
    if _cache_safe_behavior_tree("EXERCISE_MULTIPLE_CHOICE", frozen):
        residues.append("frozen direct-solve exercise BT is cache-safe")
    if not _cache_safe_behavior_tree("EXERCISE_TEXT_INPUT", generic):
        residues.append("generic send_to_llm exercise BT is not cache-safe")
    residues.extend(
        _require_source(
            ctx,
            "spark/tasks/variant_cache.py",
            (
                "variant == \"UNKNOWN\"",
                "NOT storing BT for bare master",
                "NOT storing BT for transition",
                "frozen answer guard",
                "lookup_variant_bt_refused_transition",
            ),
        )
    )
    return residues


def _check_rejection_capture_artifacts(ctx: ProbeContext) -> list[str]:
    residues = _require_source(
        ctx,
        "spark/worker/bt_generator.py",
        (
            "WORKER_RAW_RESPONSE_NAME",
            "WORKER_RAW_STDOUT_NAME",
            "rejected_bt.json",
            "failure_kind=\"conformance_rejection\"",
        ),
    )
    residues.extend(
        _require_source(
            ctx,
            "spark/worker/consultation_worker.py",
            (
                "\"_rejected_bt_path\"",
                "\"_worker_raw_response_path\"",
                "\"_worker_raw_stdout_path\"",
                "\"worker_failed\"",
            ),
        )
    )
    residues.extend(
        _require_source(
            ctx,
            "spark/tasks/escalation.py",
            (
                "rejected_bt_dst",
                "worker_raw_response_dst",
                "worker_raw_stdout_dst",
                "worker BT rejected by conformance",
            ),
        )
    )
    if not (ctx.corpus / "consults/consult_conformance_smoke_1989393_3/rejected_bt.json").exists():
        residues.append("recorded conformance rejection fixture missing")
    return residues


def _check_billing_contract_registered(ctx: ProbeContext) -> list[str]:
    return _require_source(
        ctx,
        "spark/storage/credits.py",
            (
                "def debit_screen(",
                "Debit on successful screen completion",
                "idempotency_key=key",
            ),
    )


def _check_unknown_never_worker(ctx: ProbeContext) -> list[str]:
    residues = _require_source(
        ctx,
        "spark/routes/next_action.py",
        (
            "UNKNOWN must NEVER reach the worker",
            "if screen_type == \"UNKNOWN\" or variant == \"UNKNOWN\":",
            "worker consultation forbidden by R8.8 / 557bfa8",
            "_escalate_to_claude_diagnosing(",
            "attempt_key=f\"unknown:{skel_hash[:16]}\"",
        ),
    )
    text = _source(ctx, "spark/routes/next_action.py")
    branch_at = text.find("if screen_type == \"UNKNOWN\" or variant == \"UNKNOWN\":")
    later_worker_call = text.find("request_consultation(", branch_at)
    later_build_call = text.find("return _build_screen_directive(", branch_at)
    if branch_at < 0:
        return residues
    if later_worker_call >= 0 and (later_build_call < 0 or later_worker_call < later_build_call):
        residues.append("Step 5D UNKNOWN branch still reaches request_consultation before _build_screen_directive")
    return residues


def _check_handler_param_manifest_v2(ctx: ProbeContext) -> list[str]:
    residues = _require_source(
        ctx,
        "app/tasks/bt_handlers.py",
        (
            "params.get(\"option\") or params.get(\"option_text\") or params.get(\"target\")",
            "params.get(\"trigger_element\") or params.get(\"element\")",
            "params.get(\"trigger_target\")",
            "params.get(\"combobox\")",
            "trigger_match_mode = params.get(\"trigger_match_mode\", \"contains\")",
            "strategies = params.get(\"strategies\") or [",
        ),
    )
    if len(V2_SELECT_DROPDOWN_PARAM_SURFACE) < 11:
        residues.append("V2 select_dropdown_option param surface is below 11 slots")

    handler_source = _source(ctx, "app/tasks/bt_handlers.py")
    try:
        tree = ast.parse(handler_source)
    except SyntaxError as exc:
        return residues + [f"bt_handlers.py AST parse failed: {exc}"]
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Attribute)
            and node.attr == "blackboard"
            and isinstance(node.value, ast.Name)
            and node.value.id == "ctx"
        ):
            residues.append(f"handler source directly touches ctx.blackboard at line {node.lineno}")
    residues.extend(
        _require_source(
            ctx,
            "app/tasks/bt_core.py",
            (
                "params = ctx.blackboard.resolve_params(raw_params)",
                "ctx.blackboard.set(store_key, result)",
                "ctx.blackboard.set(\"_continue_loop\", True)",
            ),
        )
    )
    return residues


def _check_paths_fail_loud_env(ctx: ProbeContext) -> list[str]:
    residues = _require_source(
        ctx,
        "spark/tasks/paths.py",
        (
            "TAEY_ED_DATA_DIR is required",
            "refusing to use an implicit runtime data root",
        ),
    )
    paths_source = _source(ctx, "spark/tasks/paths.py")
    if "return Path(\"/home/user/taey-ed-data\")" in paths_source:
        residues.append("paths.py still contains the implicit /home/user data-root fallback")

    env = os.environ.copy()
    env.pop("TAEY_ED_DATA_DIR", None)
    env["PYTHONPATH"] = str(ctx.repo_root)
    proc = subprocess.run(
        [sys.executable, "-c", "import spark.tasks.paths"],
        cwd=str(ctx.repo_root),
        env=env,
        text=True,
        capture_output=True,
        timeout=10,
    )
    combined = f"{proc.stdout}\n{proc.stderr}"
    if proc.returncode == 0:
        residues.append("env-free spark.tasks.paths import succeeded")
    if "TAEY_ED_DATA_DIR" not in combined:
        residues.append("env-free paths failure did not name TAEY_ED_DATA_DIR")
    if "/home/user/taey-ed-data" in combined:
        residues.append("env-free paths failure still references the implicit data root")
    return residues


def _check_recipe_ast_registered(ctx: ProbeContext) -> list[str]:
    return _require_source(
        ctx,
        "spark/tasks/screen_type_assembler.py",
        (
            "sections = _split_top_level_sections",
            "recipe_text = sections.get(\"recipe\", \"\")",
            "allowed_actions = _recipe_actions(recipe_text)",
        ),
    )


def _check_per_type_solve_mode(ctx: ProbeContext) -> list[str]:
    return _require_source(
        ctx,
        "spark/tasks/screen_type_assembler.py",
        (
            "when a recipe prescribes the runtime",
            "get_master_category(artifact[\"screen_type\"]) == \"EXERCISE\"",
            "\"send_to_llm\" in allowed_actions",
            "EXERCISE BT missing send_to_llm",
        ),
    )


def _check_dropdown_opts_gate_fresh_ref(ctx: ProbeContext) -> list[str]:
    return _require_source(
        ctx,
        "spark/platforms/khan_academy/screen_types/EXERCISE_DROPDOWN.yaml",
        (
            "NON-EMPTY GATE",
            "condition: $opts_<i>",
            "discover_menu: {role: AXMenuItem, store: opts_<i>}",
            "find_all: {role: AXComboBox, store: popups_sel_<i>}",
            "trigger_element: $popups_sel_<i>.<i>.element",
            "select_dropdown_option:",
        ),
    )


def _check_no_revert_resurrection(ctx: ProbeContext) -> list[str]:
    return _require_source(
        ctx,
        "spark/tasks/screen_type_assembler.py",
        (
            "is_staging_cycle = (",
            "actual_actions <= _STAGING_ACTIONS",
            "and \"scroll\" in allowed_actions",
            "if not is_staging_cycle:",
        ),
    )


CHECKS: dict[str, Callable[[ProbeContext], list[str]]] = {
    "manifest_conditional_red_fixture": _check_manifest_conditional_red_fixture,
    "manifest_composable_contract": _check_manifest_composable_contract,
    "served_lint_before_normalizer": _check_served_lint_before_normalizer,
    "expected_next_seed_never_veto": _check_expected_next_seed_never_veto,
    "validated_store_source": _check_validated_store_source,
    "worker_handoff_receipt": _check_worker_handoff_receipt,
    "worker_output_schema_retry": _check_worker_output_schema_retry,
    "engine_primitives": _check_engine_primitives,
    "state_ladder_liveness": _check_state_ladder_liveness,
    "yaml_fold_reset": _check_yaml_fold_reset,
    "escalation_auto_dispatch": _check_escalation_auto_dispatch,
    "canonical_variant_lookup": _check_canonical_variant_lookup,
    "store_poison_guards": _check_store_poison_guards,
    "rejection_capture_artifacts": _check_rejection_capture_artifacts,
    "billing_contract_registered": _check_billing_contract_registered,
    "unknown_never_worker": _check_unknown_never_worker,
    "handler_param_manifest_v2": _check_handler_param_manifest_v2,
    "paths_fail_loud_env": _check_paths_fail_loud_env,
    "recipe_ast_registered": _check_recipe_ast_registered,
    "per_type_solve_mode": _check_per_type_solve_mode,
    "dropdown_opts_gate_fresh_ref": _check_dropdown_opts_gate_fresh_ref,
    "no_revert_resurrection": _check_no_revert_resurrection,
}
