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
from typing import Literal, TextIO, assert_never

from .errors import ValidationFinding
from .playbook_schema import discover_playbook_schemas
from .research import discover_research_files, validate_research_file
from .screened import discover_screened_files, validate_screened_file
from .view import discover_view_files, validate_view_file

type ValidationTarget = Literal["screened", "view", "research"]
_TARGETS: tuple[ValidationTarget, ...] = ("screened", "view", "research")


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

    # research validation で playbook schema lookup が必要。1 回だけ discover
    # して全 research file に再利用する (Phase 2 で packet が増えたときに
    # I/O を線形回数に抑える)。
    known_playbooks = frozenset(discover_playbook_schemas(root / "playbooks"))

    findings: list[ValidationFinding] = []
    file_count = 0
    for target in targets:
        files = _discover(root, target)
        file_count += len(files)
        for path in files:
            findings.extend(_validate(root, target, path, known_playbooks))

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
        case "screened":
            return discover_screened_files(root / "screened")
        case "view":
            return discover_view_files(root / "view")
        case "research":
            return discover_research_files(root / "research")
        case _ as unhandled:  # pragma: no cover
            assert_never(unhandled)


def _validate(
    root: Path,
    target: ValidationTarget,
    path: Path,
    known_playbooks: frozenset[str],
) -> list[ValidationFinding]:
    match target:
        case "screened":
            return validate_screened_file(path)
        case "view":
            return validate_view_file(path)
        case "research":
            return validate_research_file(
                path,
                playbooks_root=root / "playbooks",
                known_playbooks=known_playbooks,
            )
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
