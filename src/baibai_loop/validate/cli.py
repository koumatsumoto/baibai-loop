"""CLI entry point for ``baibai-loop-validate``.

Walks ``<root>/screened/``, ``<root>/view/`` and ``<root>/research/`` and
reports every ValidationFinding produced by the per-artefact validators. CI
runs this command against the repository to gate merges on schema regressions.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import TextIO

from .errors import ValidationFinding
from .research import discover_research_files, validate_research_file
from .screened import discover_screened_files, validate_screened_file
from .view import discover_view_files, validate_view_file

_TARGETS: tuple[str, ...] = ("screened", "view", "research")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="baibai-loop-validate")
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
    parser.add_argument(
        "--all",
        action="store_true",
        help="(default) validate every supported artefact type; kept for "
        "compatibility with `baibai-loop-validate --all` in docs",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    targets: tuple[str, ...] = tuple(args.target) if args.target else _TARGETS
    return run_validation(
        root=args.root,
        targets=targets,
        stdout=sys.stdout,
        stderr=sys.stderr,
    )


def run_validation(
    *,
    root: Path,
    targets: Sequence[str],
    stdout: TextIO,
    stderr: TextIO,
) -> int:
    findings: list[ValidationFinding] = []
    file_count = 0
    for target in targets:
        files = _discover(root, target)
        file_count += len(files)
        for path in files:
            findings.extend(_validate(root, target, path))

    error_count = 0
    warning_count = 0
    for finding in findings:
        line = _format_finding(finding, root)
        if finding.severity == "error":
            error_count += 1
        else:
            warning_count += 1
        print(line, file=stderr)

    summary = f"validated {file_count} file(s): {error_count} error(s), {warning_count} warning(s)"
    print(summary, file=stdout)
    return 1 if error_count else 0


def _discover(root: Path, target: str) -> list[Path]:
    if target == "screened":
        return discover_screened_files(root / "screened")
    if target == "view":
        return discover_view_files(root / "view")
    if target == "research":
        return discover_research_files(root / "research")
    raise AssertionError(f"unreachable target: {target!r}")


def _validate(root: Path, target: str, path: Path) -> list[ValidationFinding]:
    if target == "screened":
        return validate_screened_file(path)
    if target == "view":
        return validate_view_file(path)
    if target == "research":
        return validate_research_file(path, playbooks_root=root / "playbooks")
    raise AssertionError(f"unreachable target: {target!r}")


def _format_finding(finding: ValidationFinding, root: Path) -> str:
    try:
        target = finding.target.relative_to(root)
    except ValueError:
        target = finding.target
    suffix = f" @ {finding.location}" if finding.location else ""
    return f"[{finding.severity}] {target}: {finding.code}{suffix} — {finding.message}"


if __name__ == "__main__":
    raise SystemExit(main())
