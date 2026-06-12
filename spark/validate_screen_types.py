#!/usr/bin/env python3
"""Validate per-screen-type YAML files against Jesse's screen-type contract."""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable


REQUIRED_SECTIONS = [
    "screen_type",
    "classify",
    "recipe",
    "contracts",
    "actuation",
    "verification",
    "completion",
    "failure_modes",
]

MAX_RENDERED_CHARS = 4000
NEGATION_MARKERS = ("never", "do not", "don't", "not ", "banned", "forbidden", "no ")


@dataclass
class ValidationIssue:
    path: Path
    message: str
    line_no: int | None = None

    def render(self) -> str:
        if self.line_no is None:
            return f"{self.path}: {self.message}"
        return f"{self.path}:{self.line_no}: {self.message}"


@dataclass
class ValidationResult:
    path: Path
    issues: list[ValidationIssue] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.issues


def _iter_yaml_paths(target: Path) -> Iterable[Path]:
    if target.is_file():
        if target.suffix.lower() != ".yaml":
            raise ValueError(f"expected a .yaml file, got {target}")
        yield target
        return
    if not target.is_dir():
        raise ValueError(f"path does not exist: {target}")
    yield from sorted(p for p in target.glob("*.yaml") if p.is_file())


def _is_browser_strategy_violation(text: str) -> bool:
    lowered = text.lower()
    if "strategy:" not in lowered:
        return False
    strategy_value = lowered.split("strategy:", 1)[1].strip()
    return strategy_value.startswith("focus_space")


def _is_flat_drag_key_violation(text: str) -> bool:
    stripped = text.strip().lower()
    return stripped.startswith(("start_x:", "from_x:", "to_x:"))


def _is_ax_press_browser_violation(text: str) -> bool:
    lowered = text.lower()
    if "ax_press" not in lowered:
        return False
    if any(marker in lowered for marker in NEGATION_MARKERS):
        return False
    return "strategy:" in lowered or lowered.startswith("- ax_press") or lowered.startswith("ax_press:")


def _is_try_again_retry_violation(text: str) -> bool:
    lowered = text.lower()
    if "try again" not in lowered:
        return False
    if "modal-dismiss-only" in lowered or "state signal" in lowered or "not an affordance" in lowered:
        return False
    if any(marker in lowered for marker in NEGATION_MARKERS):
        return False
    return any(token in lowered for token in ("find_and_click", "click ", "click_", "press ", "retry", "re-ask"))


def _is_reask_or_blind_retry_violation(text: str) -> bool:
    lowered = text.lower()
    for token in ("blind retry", "re-ask", "reset-and-retry"):
        if token in lowered and not any(marker + token in lowered for marker in ("no ", "never ", "don't ", "do not ")):
            return True
    return False


def _lint_lines(path: Path, text: str) -> list[ValidationIssue]:
    issues: list[ValidationIssue] = []
    for idx, line in enumerate(text.splitlines(), start=1):
        if "match_mode: contains" in line:
            issues.append(ValidationIssue(path, "forbidden token: match_mode: contains", idx))
        if _is_browser_strategy_violation(line):
            issues.append(ValidationIssue(path, "browser strategy must not use focus_space", idx))
        if _is_flat_drag_key_violation(line):
            issues.append(ValidationIssue(path, "flat drag keys are forbidden; use nested start/end objects", idx))
        if _is_ax_press_browser_violation(line):
            issues.append(ValidationIssue(path, "browser elements must not use ax_press", idx))
        if _is_try_again_retry_violation(line):
            issues.append(ValidationIssue(path, "\"Try again\" recovery recipes are banned by R3", idx))
        if _is_reask_or_blind_retry_violation(line):
            issues.append(ValidationIssue(path, "wrong-answer retry language is banned by R3", idx))
    return issues


def _extract_top_level_sections(text: str) -> list[str]:
    sections: list[str] = []
    for line in text.splitlines():
        if not line or line.startswith((" ", "\t", "#")):
            continue
        if ":" not in line:
            continue
        key = line.split(":", 1)[0].strip()
        if key:
            sections.append(key)
    return sections


def _extract_screen_type(text: str) -> str | None:
    for line in text.splitlines():
        if line.startswith("screen_type:"):
            return line.split(":", 1)[1].strip()
    return None


def validate_yaml_file(path: Path) -> ValidationResult:
    result = ValidationResult(path=path)

    try:
        text = path.read_text()
    except OSError as exc:
        result.issues.append(ValidationIssue(path, f"read failed: {exc}"))
        return result

    rendered_chars = len(text)
    if rendered_chars > MAX_RENDERED_CHARS:
        result.issues.append(
            ValidationIssue(path, f"one-page budget exceeded: {rendered_chars} chars > {MAX_RENDERED_CHARS}")
        )

    sections = _extract_top_level_sections(text)
    section_set = set(sections)

    for section in REQUIRED_SECTIONS:
        if section not in section_set:
            result.issues.append(ValidationIssue(path, f"missing required section: {section}"))

    for section in REQUIRED_SECTIONS:
        if sections.count(section) > 1:
            result.issues.append(ValidationIssue(path, f"duplicate top-level section: {section}"))

    expected_name = path.stem
    actual_name = _extract_screen_type(text)
    if actual_name != expected_name:
        result.issues.append(
            ValidationIssue(path, f"screen_type mismatch: expected {expected_name!r}, got {actual_name!r}")
        )

    result.issues.extend(_lint_lines(path, text))
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Validate screen_types/*.yaml files against the one-page screen-type schema."
    )
    parser.add_argument("target", help="A .yaml file or a directory containing .yaml files")
    args = parser.parse_args(argv)

    target = Path(args.target)
    try:
        yaml_paths = list(_iter_yaml_paths(target))
    except ValueError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2

    if not yaml_paths:
        print(f"ERROR: no .yaml files found under {target}", file=sys.stderr)
        return 2

    results = [validate_yaml_file(path) for path in yaml_paths]
    issue_count = sum(len(r.issues) for r in results)

    for result in results:
        status = "OK" if result.ok else f"FAIL ({len(result.issues)} issues)"
        print(f"{status}: {result.path}")
        for issue in result.issues:
            print(f"  - {issue.render()}")

    ok_files = sum(1 for result in results if result.ok)
    print(
        f"\nSummary: {ok_files}/{len(results)} files passed, {issue_count} total issue(s)."
    )
    return 0 if issue_count == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
