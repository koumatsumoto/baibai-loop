"""CLI entry point for ``baibai-loop-validation``.

Walks the records tree under ``<root>/records/`` and reports every
ValidationFinding produced by the per-artefact validators. CI runs this
command against the repository to gate merges on schema regressions.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Literal, TextIO, assert_never

from baibai_loop.foundation.errors import ValidationFinding

from .benchmark_observation import (
    discover_benchmark_observation_files,
    validate_benchmark_observation_file,
)
from .candidates import discover_candidates_files, validate_candidates_file
from .decision_packet import discover_decision_packet_files, validate_decision_packet_file
from .execution_lifecycle import (
    discover_execution_lifecycle_files,
    validate_execution_lifecycle_file,
)
from .holding_review import discover_holding_review_files, validate_holding_review_file
from .ledger import discover_ledger_files, validate_ledger_file
from .macro_context import discover_macro_context_files, validate_macro_context_file
from .policy import validate_policy_file
from .portfolio_outcome import discover_portfolio_outcome_files, validate_portfolio_outcome_file

type ValidationTarget = Literal[
    "macro-context",
    "policy",
    "candidates",
    "ledger",
    "execution-lifecycle",
    "decision-packet",
    "holding-review",
    "benchmark-observation",
    "portfolio-outcome",
]
_TARGETS: tuple[ValidationTarget, ...] = (
    "macro-context",
    "policy",
    "candidates",
    "ledger",
    "execution-lifecycle",
    "decision-packet",
    "holding-review",
    "benchmark-observation",
    "portfolio-outcome",
)

MACRO_CONTEXT_ROOT = Path("records/01-macro-context")
POLICY_PATH = Path("docs/portfolio-management.md")
CANDIDATES_ROOT = Path("records/02-candidates")
THESIS_ROOT = Path("records/03-thesis")
POSITION_ROOT = Path("records/04-position")
BENCHMARK_ROOT = POSITION_ROOT / "benchmarks"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="baibai-loop-validation")
    parser.add_argument(
        "--root",
        type=Path,
        default=Path.cwd(),
        help="repository root (defaults to current working directory)",
    )
    parser.add_argument(
        "--target",
        action="append",
        choices=_TARGETS,
        help="restrict validation to a specific artefact type (repeatable)",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    targets: tuple[ValidationTarget, ...] = tuple(args.target) if args.target else _TARGETS
    return run_validation(
        root=args.root,
        targets=targets,
        stdout=sys.stdout,
        stderr=sys.stderr,
    )


def run_validation(
    *,
    root: Path,
    targets: Sequence[ValidationTarget],
    stdout: TextIO,
    stderr: TextIO,
) -> int:
    if not root.is_dir():
        # silent failure 防止: typo した --root で「0 file validated」になり
        # CI が誤って通るのを防ぐ。
        print(
            f"--root path does not exist or is not a directory: {root}",
            file=stderr,
        )
        return 1

    findings: list[ValidationFinding] = []
    file_count = 0
    for target in targets:
        files = _discover(root, target)
        file_count += len(files)
        for path in files:
            findings.extend(_validate(target, path))

    error_count = 0
    warning_count = 0
    for finding in findings:
        line = _format_finding(finding, root)
        match finding.severity:
            case "error":
                error_count += 1
            case "warning":
                warning_count += 1
            case _ as unhandled:  # pragma: no cover
                assert_never(unhandled)
        print(line, file=stderr)

    summary = f"validated {file_count} file(s): {error_count} error(s), {warning_count} warning(s)"
    print(summary, file=stdout)
    return 1 if error_count else 0


def _discover(root: Path, target: ValidationTarget) -> list[Path]:
    match target:
        case "macro-context":
            return discover_macro_context_files(root / MACRO_CONTEXT_ROOT)
        case "policy":
            return [root / POLICY_PATH]
        case "candidates":
            return discover_candidates_files(root / CANDIDATES_ROOT)
        case "ledger":
            return discover_ledger_files(root / POSITION_ROOT)
        case "execution-lifecycle":
            return discover_execution_lifecycle_files(root / POSITION_ROOT)
        case "decision-packet":
            return discover_decision_packet_files(root / THESIS_ROOT)
        case "holding-review":
            return discover_holding_review_files(root / POSITION_ROOT)
        case "benchmark-observation":
            return discover_benchmark_observation_files(root / BENCHMARK_ROOT)
        case "portfolio-outcome":
            return discover_portfolio_outcome_files(root / POSITION_ROOT)
        case _ as unhandled:  # pragma: no cover
            assert_never(unhandled)


def _validate(target: ValidationTarget, path: Path) -> list[ValidationFinding]:
    match target:
        case "macro-context":
            return validate_macro_context_file(path)
        case "policy":
            return validate_policy_file(path)
        case "candidates":
            return validate_candidates_file(path)
        case "ledger":
            return validate_ledger_file(path)
        case "execution-lifecycle":
            return validate_execution_lifecycle_file(path)
        case "decision-packet":
            return validate_decision_packet_file(path)
        case "holding-review":
            return validate_holding_review_file(path)
        case "benchmark-observation":
            return validate_benchmark_observation_file(path)
        case "portfolio-outcome":
            return validate_portfolio_outcome_file(path)
        case _ as unhandled:  # pragma: no cover
            assert_never(unhandled)


def _format_finding(finding: ValidationFinding, root: Path) -> str:
    try:
        target = finding.target.relative_to(root)
    except ValueError:
        target = finding.target
    suffix = f" @ {finding.location}" if finding.location else ""
    return f"[{finding.severity}] {target}: {finding.code}{suffix} — {finding.message}"


if __name__ == "__main__":
    raise SystemExit(main())
