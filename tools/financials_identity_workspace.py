"""旧workspace全体を監査保全し、completedの運用投影だけを新directoryへ復元する。"""

import argparse
import hashlib
import json
import shutil
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml

from baibai_engine.investment_judgment.workflow.selection_evidence import read_selection_evidence
from baibai_engine.investment_judgment.workflow.workspace.selected_prepare import (
    resume_selected_workspace,
)


def _file_sha256(path: Path) -> str:
    with path.open("rb") as source:
        return hashlib.file_digest(source, "sha256").hexdigest()


def _files(root: Path) -> dict[str, str]:
    if root.is_symlink() or any(path.is_symlink() for path in root.rglob("*")):
        raise ValueError("workspace archive requires regular files without symlinks")
    return {
        str(path.relative_to(root)): _file_sha256(path) for path in root.rglob("*") if path.is_file()
    }


def prepare_workspace(
    *,
    db: Path,
    operation_id: str,
    proposal_id: str,
    source: Path,
    archive: Path,
    output: Path,
    now: datetime,
) -> dict[str, Any]:
    """旧著作物を新manifest下へ持込まず、元indexの解決先を一組で保持する。"""
    receipt_path = archive.parent / (archive.name + "-receipt.json")
    if archive.exists() or output.exists() or receipt_path.exists():
        raise ValueError("workspace cutover destinations must not exist")
    for target in (archive, output):
        if source.resolve() == target.resolve() or source.resolve() in target.resolve().parents:
            raise ValueError("workspace cutover must not write inside its source")
    if (
        archive.resolve() == output.resolve()
        or archive.resolve() in output.resolve().parents
        or output.resolve() in archive.resolve().parents
    ):
        raise ValueError("archive and operational workspace must be separate")
    manifest = yaml.safe_load((source / "manifest.yaml").read_text())
    if not isinstance(manifest, dict) or manifest.get("operation_id") != operation_id:
        raise ValueError("workspace belongs to another Operation")
    evidence = read_selection_evidence(
        operation_id=operation_id,
        sha256=manifest["selection_sha256"],
        proposal_id=proposal_id,
        db_path=db,
    )
    if evidence["role"] != "as_published":
        raise ValueError("workspace does not refer to a migrated publication")
    before = _files(source)
    shutil.copytree(source, archive)
    prepared = resume_selected_workspace(
        operation_id=operation_id,
        db_path=db,
        workspace=output,
        now=now,
        company_growth_periods=manifest.get("company_growth_periods", 12),
    )
    if prepared.status != "already_completed":
        raise ValueError("workspace migration cannot resume an active Operation")
    if before != _files(source) or before != _files(archive):
        raise ValueError("original workspace bytes changed during archival")
    result = {
        "operation_id": operation_id,
        "published_selection_sha256": evidence["selection_sha256"],
        "operational_selection_sha256": prepared.selection_sha256,
        "original_files": before,
        "operational_files": _files(output),
        "archive": str(archive.resolve()),
        "operational_workspace": str(output.resolve()),
        "old_indices_resolve_only_against": "original domain-inputs and published canonical",
    }
    with receipt_path.open("x", encoding="utf-8") as receipt:
        receipt.write(json.dumps(result, ensure_ascii=False, indent=2))
    return result


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, required=True)
    parser.add_argument("--operation-id", required=True)
    parser.add_argument("--proposal-id", required=True)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--archive", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    result = prepare_workspace(
        db=args.db,
        operation_id=args.operation_id,
        proposal_id=args.proposal_id,
        source=args.source,
        archive=args.archive,
        output=args.output,
        now=datetime.now(UTC),
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
