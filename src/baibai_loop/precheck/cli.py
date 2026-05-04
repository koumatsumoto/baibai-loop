"""CLI entry point for ``baibai-loop-precheck``."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import TextIO

from .source_refs import OutlookFinding, scan_outlook_source_refs

OUTLOOK_ROOT = Path("records/02-outlook")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="baibai-loop-precheck",
        description=(
            "Mechanized self-review for anti-patterns. Currently scans outlook "
            "rationale ↔ source_refs token consistency (AP-06)."
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
    findings = scan_outlook_source_refs(root / OUTLOOK_ROOT, repo_root=root)
    for finding in findings:
        print(_format_finding(finding, root), file=stderr)
    summary = f"precheck: {len(findings)} finding(s)"
    print(summary, file=stdout)
    return 1 if strict and findings else 0


def _format_finding(finding: OutlookFinding, root: Path) -> str:
    try:
        target = finding.target.relative_to(root)
    except ValueError:
        target = finding.target
    return f"[{finding.severity}] {target}: {finding.code} @ {finding.location} — {finding.message}"


if __name__ == "__main__":
    raise SystemExit(main())
