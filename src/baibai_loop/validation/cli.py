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
from baibai_loop.thesis import (
    discover_thesis_files,
    load_thesis_document,
    validate_thesis_collection,
    validate_thesis_file,
    validate_thesis_parsed,
)
from baibai_loop.thesis.playbook_schema import discover_playbook_schemas

from .candidates import discover_candidates_files, validate_candidates_file
from .decisions import discover_decisions_files, validate_decisions_file
from .macro_context import discover_macro_context_files, validate_macro_context_file
from .policy import validate_policy_file
from .position import discover_position_files, validate_position_file

type ValidationTarget = Literal[
    "macro-context",
    "policy",
    "candidates",
    "thesis",
    "position",
    "decisions",
]
_TARGETS: tuple[ValidationTarget, ...] = (
    "macro-context",
    "policy",
    "candidates",
    "thesis",
    "position",
    "decisions",
)

MACRO_CONTEXT_ROOT = Path("records/01-macro-context")
POLICY_PATH = Path("docs/portfolio-policy.md")
CANDIDATES_ROOT = Path("records/04-candidates")
THESIS_ROOT = Path("records/05-thesis")
POSITION_ROOT = Path("records/06-position")
DECISIONS_ROOT = Path("records/_decisions")
PLAYBOOKS_ROOT = Path("records/_playbooks")


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

    # thesis validation で playbook schema lookup が必要。1 回だけ discover
    # して全 thesis file に再利用する (Phase 2 で packet が増えたときに
    # I/O を線形回数に抑える)。
    known_playbooks = frozenset(discover_playbook_schemas(root / PLAYBOOKS_ROOT))

    # thesis target は per-file 検証と collection 集約の両方で同じ document を
    # 読むため、target ループ前に 1 度 load して再利用する。
    thesis_documents: dict[Path, tuple[dict[str, object], str] | list[ValidationFinding]] = {}
    if "thesis" in targets:
        for path in discover_thesis_files(root / THESIS_ROOT):
            thesis_documents[path] = load_thesis_document(path)

    findings: list[ValidationFinding] = []
    file_count = 0
    for target in targets:
        files = _discover(root, target)
        file_count += len(files)
        for path in files:
            if target == "thesis":
                doc = thesis_documents[path]
                if isinstance(doc, list):
                    findings.extend(doc)
                else:
                    front_matter, body = doc
                    findings.extend(
                        validate_thesis_parsed(
                            path,
                            front_matter,
                            body,
                            playbooks_root=root / PLAYBOOKS_ROOT,
                            known_playbooks=known_playbooks,
                        )
                    )
            else:
                findings.extend(_validate(root, target, path, known_playbooks))
    if thesis_documents:
        front_matters = [
            (path, doc[0]) for path, doc in thesis_documents.items() if not isinstance(doc, list)
        ]
        findings.extend(validate_thesis_collection(front_matters))

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
        case "thesis":
            return discover_thesis_files(root / THESIS_ROOT)
        case "position":
            return discover_position_files(root / POSITION_ROOT)
        case "decisions":
            return discover_decisions_files(root / DECISIONS_ROOT)
        case _ as unhandled:  # pragma: no cover
            assert_never(unhandled)


def _validate(
    root: Path,
    target: ValidationTarget,
    path: Path,
    known_playbooks: frozenset[str],
) -> list[ValidationFinding]:
    match target:
        case "macro-context":
            return validate_macro_context_file(path)
        case "policy":
            return validate_policy_file(path)
        case "candidates":
            return validate_candidates_file(path)
        case "thesis":
            return validate_thesis_file(
                path,
                playbooks_root=root / PLAYBOOKS_ROOT,
                known_playbooks=known_playbooks,
            )
        case "position":
            return validate_position_file(path)
        case "decisions":
            return validate_decisions_file(path)
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
