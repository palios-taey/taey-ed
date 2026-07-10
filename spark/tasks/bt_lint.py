"""Behavior-tree lint against the pinned Mac executor manifest."""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from spark.tasks.executor_manifest import (
    ALL_ACTIONS,
    COMPOSABLE_ACTIONS,
    EXECUTOR_MANIFEST,
    NODE_TYPES,
    REGISTERED_ACTIONS,
)


@dataclass(frozen=True)
class LintViolation:
    rule: str
    path: str
    message: str


@dataclass(frozen=True)
class LintResult:
    ok: bool
    manifest_hash: str
    violations: tuple[LintViolation, ...]


NODE_TYPE_SET = set(NODE_TYPES)
COMPOSABLE_ACTION_SET = set(COMPOSABLE_ACTIONS)
REGISTERED_ACTION_SET = set(REGISTERED_ACTIONS)
ALL_ACTION_SET = set(ALL_ACTIONS)
REF_RE = re.compile(r"^\$[A-Za-z_][A-Za-z0-9_]*(?:\.(?:[A-Za-z_][A-Za-z0-9_]*|\d+))*$")


def lint_bt(tree: Any) -> LintResult:
    violations: list[LintViolation] = []
    _lint_node(tree, "tree", violations)
    return LintResult(
        ok=not violations,
        manifest_hash=str(EXECUTOR_MANIFEST["bundle_hash"]),
        violations=tuple(violations),
    )


def summarize_violations(result: LintResult, *, limit: int = 3) -> str:
    parts = [
        f"{v.rule} {v.path}: {v.message}"
        for v in result.violations[:limit]
    ]
    if len(result.violations) > limit:
        parts.append(f"... +{len(result.violations) - limit} more")
    return "; ".join(parts)


def violation_hash(result: LintResult) -> str:
    payload = {
        "manifest_hash": result.manifest_hash,
        "violations": [asdict(v) for v in result.violations],
    }
    encoded = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def write_lint_audit(
    *,
    result: LintResult,
    tree: Any,
    source: str,
    context: dict[str, Any] | None = None,
    audit_dir: Path | str | None = None,
) -> Path:
    root = Path(audit_dir) if audit_dir else _default_audit_dir()
    root.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    path = root / f"{timestamp}_{source}_{uuid.uuid4().hex[:8]}.json"
    payload = {
        "recorded_at": datetime.now(timezone.utc).isoformat(),
        "source": source,
        "context": context or {},
        "manifest": {
            "bundle_hash": EXECUTOR_MANIFEST["bundle_hash"],
            "bundle_hash_basis": EXECUTOR_MANIFEST["bundle_hash_basis"],
            "bundle_source": EXECUTOR_MANIFEST["bundle_source"],
            "bundle_files": EXECUTOR_MANIFEST["bundle_files"],
            "schema_version": EXECUTOR_MANIFEST["schema_version"],
            "manifest_source": EXECUTOR_MANIFEST["manifest_source"],
        },
        "violations": [asdict(v) for v in result.violations],
        "tree": tree,
    }
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=True), encoding="utf-8")
    return path


def _default_audit_dir() -> Path:
    from spark.tasks.paths import CONSULTATIONS_DIR

    return CONSULTATIONS_DIR / "BT_LINT_REJECTIONS"


def _lint_node(node: Any, path: str, violations: list[LintViolation]) -> None:
    if isinstance(node, list):
        violations.append(
            LintViolation(
                "M8.1",
                path,
                "node is a list; wrap multiple nodes in a sequence children list",
            )
        )
        for index, child in enumerate(node):
            _lint_node(child, f"{path}[{index}]", violations)
        return
    if not isinstance(node, dict):
        violations.append(
            LintViolation("M8.1", path, f"node must be an object, got {type(node).__name__}")
        )
        return

    raw_type = node.get("type")
    node_type = raw_type or "action"
    action = node.get("action")

    if raw_type is not None and raw_type not in NODE_TYPE_SET:
        violations.append(
            LintViolation(
                "M8.1",
                path,
                f"type {raw_type!r} is not in {sorted(NODE_TYPE_SET)}",
            )
        )
    if raw_type in COMPOSABLE_ACTION_SET:
        violations.append(
            LintViolation(
                "M8.2",
                path,
                f"composable {raw_type!r} must be emitted as type='action', action={raw_type!r}",
            )
        )

    if node_type == "action":
        _lint_action_node(node, action, path, violations)

    _lint_store_keys(node, node_type, action, path, violations)
    _lint_refs(node, path, violations)
    _lint_child_shapes(node, action, path, violations)

    for index, child in enumerate(node.get("children") or []):
        _lint_node(child, f"{path}.children[{index}]", violations)
    for key in ("do", "then", "else"):
        if isinstance(node.get(key), dict):
            _lint_node(node[key], f"{path}.{key}", violations)
        elif isinstance(node.get(key), list):
            for index, child in enumerate(node[key]):
                _lint_node(child, f"{path}.{key}[{index}]", violations)


def _lint_action_node(
    node: dict[str, Any],
    action: Any,
    path: str,
    violations: list[LintViolation],
) -> None:
    if not isinstance(action, str) or not action:
        violations.append(
            LintViolation("M8.3", path, "action node must carry a non-empty action name")
        )
        return
    if action in COMPOSABLE_ACTION_SET:
        explicit_type = node.get("type")
        if explicit_type not in (None, "action"):
            violations.append(
                LintViolation(
                    "M8.2",
                    path,
                    f"composable {action!r} has invalid explicit type {explicit_type!r}",
                )
            )
        return
    if action not in REGISTERED_ACTION_SET:
        violations.append(
            LintViolation(
                "M8.3",
                path,
                f"action {action!r} is not registered in the pinned executor manifest",
            )
        )


def _lint_store_keys(
    node: dict[str, Any],
    node_type: str,
    action: Any,
    path: str,
    violations: list[LintViolation],
) -> None:
    if "store" not in node and "store_to_current" not in node:
        return
    if node_type != "action" or action in COMPOSABLE_ACTION_SET or action not in ALL_ACTION_SET:
        keys = ", ".join(k for k in ("store", "store_to_current") if k in node)
        violations.append(
            LintViolation(
                "M8.5",
                path,
                f"{keys} only apply to regular registered action nodes",
            )
        )


def _lint_refs(node: dict[str, Any], path: str, violations: list[LintViolation]) -> None:
    for key in ("items", "condition"):
        value = node.get(key)
        if isinstance(value, str) and value.startswith("$") and not _valid_ref(value):
            violations.append(_invalid_ref(path=f"{path}.{key}", value=value))
    params = node.get("params")
    if isinstance(params, dict):
        _lint_param_refs(params, f"{path}.params", violations)


def _lint_param_refs(value: Any, path: str, violations: list[LintViolation]) -> None:
    if isinstance(value, dict):
        for key, child in value.items():
            _lint_param_refs(child, f"{path}.{key}", violations)
    elif isinstance(value, list):
        for index, child in enumerate(value):
            _lint_param_refs(child, f"{path}[{index}]", violations)
    elif isinstance(value, str) and value.startswith("$") and not _valid_ref(value):
        violations.append(_invalid_ref(path=path, value=value))


def _valid_ref(value: str) -> bool:
    return bool(REF_RE.fullmatch(value))


def _invalid_ref(*, path: str, value: str) -> LintViolation:
    return LintViolation(
        "M8.4",
        path,
        (
            f"invalid blackboard ref {value!r}; manifest section 4 accepts one "
            "$var(.field|.N)* reference, not interpolation or multiple refs"
        ),
    )


def _lint_child_shapes(
    node: dict[str, Any],
    action: Any,
    path: str,
    violations: list[LintViolation],
) -> None:
    if action == "for_each":
        child = node.get("do")
        if not isinstance(child, dict):
            violations.append(
                LintViolation("M8.6", f"{path}.do", "for_each.do must be one node object")
            )
    if action == "conditional":
        for key in ("then", "else"):
            if key in node and not isinstance(node[key], dict):
                violations.append(
                    LintViolation(
                        "M8.7",
                        f"{path}.{key}",
                        f"conditional.{key} must be one node object when present",
                    )
                )
