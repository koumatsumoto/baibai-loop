"""CLI entry point for ``baibai-loop-validate``.

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

from .benchmark import discover_benchmark_manifest_files, validate_benchmark_manifest_file
from .brief import discover_brief_files, validate_brief_file
from .candidates import discover_candidates_files, validate_candidates_file
from .errors import ValidationFinding
from .ledger import discover_ledger_files, validate_ledger_file
from .outlook import discover_outlook_files, validate_outlook_file
from .playbook_schema import discover_playbook_schemas
from .policy import discover_policy_files, validate_policy_file
from .portfolio_exposure import (
    discover_portfolio_exposure_files,
    validate_portfolio_exposure_file,
)
from .research import (
    discover_research_files,
    load_research_document,
    validate_research_collection,
    validate_research_file,
    validate_research_parsed,
)
from .review import discover_review_files, validate_review_file
from .snapshots import discover_snapshot_validation_files, validate_snapshot_integrity
from .trade import discover_trade_files, validate_trade_file

type ValidationTarget = Literal[
    "brief",
    "policy",
    "benchmark",
    "candidates",
    "outlook",
    "research",
    "trade",
    "ledger",
    "review",
    "portfolio-exposure",
    "snapshots",
]
_TARGETS: tuple[ValidationTarget, ...] = (
    "brief",
    "policy",
    "benchmark",
    "candidates",
    "outlook",
    "research",
    "trade",
    "ledger",
    "review",
    "portfolio-exposure",
    "snapshots",
)

BRIEF_ROOT = Path("records/02-brief")
POLICY_ROOT = Path("records/01-policy")
BENCHMARK_ROOT = Path("records/_benchmarks")
CANDIDATES_ROOT = Path("records/04-candidates")
OUTLOOK_ROOT = Path("records/03-outlook")
RESEARCH_ROOT = Path("records/05-research")
TRADES_ROOT = Path("records/06-trades")
LEDGER_ROOT = Path("records/_ledger")
PLAYBOOKS_ROOT = Path("records/_playbooks")
REVIEWS_ROOT = Path("records/07-reviews")
PORTFOLIO_EXPOSURE_ROOT = Path("records/_portfolio-exposure")


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
    known_playbooks = frozenset(discover_playbook_schemas(root / PLAYBOOKS_ROOT))

    # research target は per-file 検証と collection 集約の両方で同じ document を
    # 読むため、target ループ前に 1 度 load して再利用する。
    research_documents: dict[Path, tuple[dict[str, object], str] | list[ValidationFinding]] = {}
    if "research" in targets:
        for path in discover_research_files(root / RESEARCH_ROOT):
            research_documents[path] = load_research_document(path)

    findings: list[ValidationFinding] = []
    file_count = 0
    for target in targets:
        files = _discover(root, target)
        file_count += len(files)
        for path in files:
            if target == "snapshots":
                continue
            if target == "research":
                doc = research_documents[path]
                if isinstance(doc, list):
                    findings.extend(doc)
                else:
                    front_matter, body = doc
                    findings.extend(
                        validate_research_parsed(
                            path,
                            front_matter,
                            body,
                            playbooks_root=root / PLAYBOOKS_ROOT,
                            known_playbooks=known_playbooks,
                        )
                    )
            else:
                findings.extend(_validate(root, target, path, known_playbooks))
        if target == "snapshots":
            findings.extend(validate_snapshot_integrity(root))
    if research_documents:
        front_matters = [
            (path, doc[0]) for path, doc in research_documents.items() if not isinstance(doc, list)
        ]
        findings.extend(validate_research_collection(front_matters))

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
        case "brief":
            return discover_brief_files(root / BRIEF_ROOT)
        case "policy":
            return discover_policy_files(root / POLICY_ROOT)
        case "benchmark":
            return discover_benchmark_manifest_files(root / BENCHMARK_ROOT)
        case "candidates":
            return discover_candidates_files(root / CANDIDATES_ROOT)
        case "outlook":
            return discover_outlook_files(root / OUTLOOK_ROOT)
        case "research":
            return discover_research_files(root / RESEARCH_ROOT)
        case "trade":
            return discover_trade_files(root / TRADES_ROOT)
        case "ledger":
            return discover_ledger_files(root / LEDGER_ROOT)
        case "review":
            return discover_review_files(root / REVIEWS_ROOT)
        case "portfolio-exposure":
            return discover_portfolio_exposure_files(root / PORTFOLIO_EXPOSURE_ROOT)
        case "snapshots":
            return discover_snapshot_validation_files(root)
        case _ as unhandled:  # pragma: no cover
            assert_never(unhandled)


def _validate(
    root: Path,
    target: ValidationTarget,
    path: Path,
    known_playbooks: frozenset[str],
) -> list[ValidationFinding]:
    match target:
        case "brief":
            return validate_brief_file(path)
        case "policy":
            return validate_policy_file(path)
        case "benchmark":
            return validate_benchmark_manifest_file(path)
        case "candidates":
            return validate_candidates_file(path)
        case "outlook":
            return validate_outlook_file(path)
        case "research":
            return validate_research_file(
                path,
                playbooks_root=root / PLAYBOOKS_ROOT,
                known_playbooks=known_playbooks,
            )
        case "trade":
            return validate_trade_file(path)
        case "ledger":
            return validate_ledger_file(path)
        case "review":
            return validate_review_file(path)
        case "portfolio-exposure":
            return validate_portfolio_exposure_file(path)
        case "snapshots":
            return []
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
