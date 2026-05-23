"""CLI entry point for ``baibai-loop-precheck``."""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import TextIO

from baibai_loop.validate.macro_context import (
    discover_macro_context_files,
    validate_macro_context_file,
)

from .decision_flip import scan_research_decision_flips

MACRO_CONTEXT_ROOT = Path("records/01-macro-context")
RESEARCH_ROOT = Path("records/05-research")


@dataclass(frozen=True, slots=True)
class _DisplayFinding:
    severity: str
    target: Path
    code: str
    message: str
    location: str


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="baibai-loop-precheck",
        description=(
            "Mechanized self-review for anti-patterns. Currently checks macro context "
            "structure and research decision flips."
        ),
    )
    parser.add_argument(
        "--root",
        type=Path,
        default=Path.cwd(),
        help="repository root (defaults to current working directory)",
    )
    parser.add_argument(
        "--strict",
        action="store_true",
        help="exit non-zero when any finding is reported (default: warnings only)",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return run_precheck(root=args.root, strict=args.strict, stdout=sys.stdout, stderr=sys.stderr)


def run_precheck(*, root: Path, strict: bool, stdout: TextIO, stderr: TextIO) -> int:
    if not root.is_dir():
        print(
            f"--root path does not exist or is not a directory: {root}",
            file=stderr,
        )
        return 1
    macro_context_findings = [
        finding
        for path in discover_macro_context_files(root / MACRO_CONTEXT_ROOT)
        for finding in validate_macro_context_file(path)
    ]
    flip_findings = scan_research_decision_flips(root / RESEARCH_ROOT, repo_root=root)
    findings: list[_DisplayFinding] = []
    for macro_finding in macro_context_findings:
        findings.append(
            _DisplayFinding(
                severity=macro_finding.severity,
                target=macro_finding.target,
                code=macro_finding.code,
                message=macro_finding.message,
                location=macro_finding.location or "",
            )
        )
    for flip_finding in flip_findings:
        findings.append(
            _DisplayFinding(
                severity=flip_finding.severity,
                target=flip_finding.target,
                code=flip_finding.code,
                message=flip_finding.message,
                location=flip_finding.location,
            )
        )
    for finding in findings:
        print(_format_finding(finding, root), file=stderr)
    summary = (
        f"precheck: {len(macro_context_findings)} macro-context finding(s), "
        f"{len(flip_findings)} decision-flip finding(s)"
    )
    print(summary, file=stdout)
    return 1 if strict and findings else 0


def _format_finding(finding: _DisplayFinding, root: Path) -> str:
    try:
        target = finding.target.relative_to(root)
    except ValueError:
        target = finding.target
    return f"[{finding.severity}] {target}: {finding.code} @ {finding.location} — {finding.message}"


if __name__ == "__main__":
    raise SystemExit(main())
