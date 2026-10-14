"""application22の必要状態を新storeへ移す一回工具。runtime互換ではない。"""

from __future__ import annotations

import argparse
import json
import shutil
import sqlite3
from contextlib import closing
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict

from baibai_engine.foundation.filesystem import write_text_atomic
from baibai_engine.foundation.yaml_io import safe_load
from baibai_engine.position.ledger import replay_events_through
from baibai_engine.position.ledger_read import load_ledger_document
from baibai_engine.research.capital_allocation import (
    StoredCapitalAllocationAssessment,
    capital_allocation_draft_sha256,
)
from baibai_engine.research.position_review import PositionReviewDocument
from baibai_engine.research.thesis_store import load_reviewed_thesis
from baibai_engine.storage.application.schema import APPLICATION_SCHEMA_VERSION, SCHEMA_SQL


class CutoverSelection(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    keep_thesis_ids: tuple[str, ...]
    keep_caa_ids: tuple[str, ...]
    keep_position_review_ids: tuple[str, ...]


@dataclass(frozen=True)
class CutoverReport:
    source_version: int
    output_version: int
    counts: dict[str, int]
    active_handoffs: int
    blocked: tuple[str, ...]


# Every unchanged table is compared again after closing the copy read transaction.
_UNCHANGED = (
    "ledger_event",
    "ledger_meta",
    "ledger_market_price",
    "task",
    "market_context_assessment",
    "market_context_assessment_head",
    "portfolio_outcome",
)
_KEYS = {
    "thesis": "thesis_id",
    "capital_allocation_assessment": "capital_allocation_assessment_id",
    "position_review": "position_review_id",
}


def _snapshot(source: sqlite3.Connection) -> dict[str, list[dict[str, Any]]]:
    if source.execute("PRAGMA user_version").fetchone()[0] != 22:
        raise ValueError("cutover source must be application schema22")
    tables = (
        *_UNCHANGED,
        "thesis",
        "thesis_review",
        "capital_allocation_assessment",
        "position_review",
        "operation_session",
    )
    return {
        table: [dict(row) for row in source.execute(f'SELECT * FROM "{table}" ORDER BY rowid')]  # nosec B608
        for table in tables
    }


def _choose(
    snapshot: dict[str, list[dict[str, Any]]], selection: CutoverSelection
) -> tuple[dict[str, list[dict[str, Any]]], list[str]]:
    copied = {table: snapshot[table] for table in _UNCHANGED}
    blocked: list[str] = []
    theses = set(selection.keep_thesis_ids)
    for table, requested in (
        ("capital_allocation_assessment", selection.keep_caa_ids),
        ("position_review", selection.keep_position_review_ids),
    ):
        key = _KEYS[table]
        indexed = {row[key]: row for row in snapshot[table]}
        missing = set(requested) - indexed.keys()
        blocked.extend(f"missing {table}: {identifier}" for identifier in sorted(missing))
        copied[table] = [row for row in snapshot[table] if row[key] in requested]
        for row in copied[table]:
            raw = json.loads(row["payload"])
            try:
                if table == "capital_allocation_assessment":
                    assessment = StoredCapitalAllocationAssessment.from_payload(raw)
                    if assessment.review.draft_sha256 != capital_allocation_draft_sha256(
                        assessment
                    ):
                        raise ValueError("CAA content review hash differs")
                    theses.update(item.thesis_id for item in assessment.alternatives)
                else:
                    position = PositionReviewDocument.model_validate(raw)
                    theses.add(position.thesis_id)
                    if row["candidate_thesis_id"] is not None:
                        theses.add(row["candidate_thesis_id"])
            except ValueError as error:
                blocked.append(f"unreadable current judgment {row[key]}: {error}")
    indexed = {row["thesis_id"]: row for row in snapshot["thesis"]}
    pending = list(theses)
    while pending:
        identifier = pending.pop()
        ancestor = indexed.get(identifier)
        if ancestor is None:
            blocked.append(f"missing thesis: {identifier}")
            continue
        parent = ancestor["supersedes_id"]
        if parent is not None and parent not in theses:
            theses.add(parent)
            pending.append(parent)
        if json.loads(ancestor["payload"]).get("schema_version") != 4:
            blocked.append(f"thesis requires new evaluation: {identifier}")
    copied["thesis"] = [row for row in snapshot["thesis"] if row["thesis_id"] in theses]
    copied["thesis_review"] = [
        row for row in snapshot["thesis_review"] if row["thesis_id"] in theses
    ]
    reviewed = {row["thesis_id"] for row in copied["thesis_review"]}
    blocked.extend(
        f"missing independent review: {identifier}" for identifier in sorted(theses - reviewed)
    )
    return copied, blocked


def _handoffs(snapshot: dict[str, list[dict[str, Any]]]) -> list[dict[str, Any]]:
    result = []
    for row in snapshot["operation_session"]:
        if row["status"] != "active":
            continue
        payload = json.loads(row["payload"])
        artifacts = payload.get("artifacts", [])
        selection: dict[str, Any] = next(
            (item for item in artifacts if item.get("kind") == "research_selection"), {}
        )
        targets = selection.get("targets", [])
        # Preserve original claims, and split mixed work without creating a V2 Operation.
        requests = [
            {
                "scope": "review_positions" if purpose == "holding" else None,
                "purpose": purpose,
                "targets": [target for target in targets if target.get("purpose") == purpose],
            }
            for purpose in ("new", "holding")
            if any(target.get("purpose") == purpose for target in targets)
        ]
        result.append(
            {
                "source_operation_id": row["operation_id"],
                "started_at": row["started_at"],
                "request": selection.get("request"),
                "requests": requests,
                "source_payload": payload,
                "requires_reselection": True,
            }
        )
    return result


def _workspace_handoffs(
    *, root: Path | None, handoffs: list[dict[str, Any]], output: Path
) -> tuple[dict[str, list[Path]], list[str]]:
    if not handoffs:
        return {}, []
    identifiers = {item["source_operation_id"] for item in handoffs}
    found: dict[str, list[Path]] = {identifier: [] for identifier in identifiers}
    if root is None or not root.is_dir():
        return found, ["active workspaces require an existing explicit --workspace-root"]
    root = root.resolve()
    blocked = []
    for manifest in root.rglob("manifest.yaml"):
        if not manifest.resolve().is_relative_to(root):
            blocked.append("workspace manifest points outside the supplied root")
            continue
        try:
            raw = safe_load(manifest.read_text())
        except (OSError, ValueError, yaml.YAMLError) as error:
            blocked.append(f"unreadable workspace manifest: {error}")
            continue
        if not isinstance(raw, dict):
            blocked.append("workspace manifest is not an object")
            continue
        identifier = raw.get("operation_id")
        if isinstance(identifier, str) and identifier in identifiers:
            found[identifier].append(manifest.parent)
    for identifier, workspaces in found.items():
        if not workspaces:
            blocked.append(f"active workspace not found in supplied root: {identifier}")
            continue
        # A bound parent already includes its holding children.
        parents = [
            path
            for path in workspaces
            if not any(path != other and path.is_relative_to(other) for other in workspaces)
        ]
        found[identifier] = parents
        for path in parents:
            if output.is_relative_to(path):
                blocked.append("output must be outside the source active workspace")
            for file in path.rglob("*"):
                if file.is_symlink() and not file.resolve().is_relative_to(path.resolve()):
                    blocked.append(f"active workspace points outside supplied root: {identifier}")
                if file.name == "manifest.yaml":
                    child = safe_load(file.read_text())
                    if not isinstance(child, dict) or child.get("operation_id") != identifier:
                        blocked.append(
                            f"nested workspace belongs to another Operation: {identifier}"
                        )
    return found, blocked


def cutover(
    *,
    source: Path,
    output: Path,
    selection: CutoverSelection,
    check: bool = False,
    workspace_root: Path | None = None,
) -> CutoverReport:
    source, output = source.resolve(), output.resolve()
    if (
        source == output
        or output.exists()
        or output.parent.joinpath(output.name + ".handoff").exists()
    ):
        raise ValueError("source must differ from a new, absent output and handoff directory")
    uri = f"{source.as_uri()}?mode=ro"
    with closing(sqlite3.connect(uri, uri=True)) as reader:
        reader.row_factory = sqlite3.Row
        reader.execute("PRAGMA query_only=ON")
        reader.execute("BEGIN")
        snapshot = _snapshot(reader)
        copied, blocked = _choose(snapshot, selection)
        handoffs = _handoffs(snapshot)
        workspaces, workspace_errors = _workspace_handoffs(
            root=workspace_root, handoffs=handoffs, output=output
        )
        blocked.extend(workspace_errors)
        # Validate exact current pairs and ledger replay using the source connection.
        for row in copied["thesis"]:
            try:
                load_reviewed_thesis(reader, row["thesis_id"])
            except ValueError as error:
                blocked.append(f"unreadable Reviewed Thesis {row['thesis_id']}: {error}")
        for row in copied["capital_allocation_assessment"]:
            try:
                assessment = StoredCapitalAllocationAssessment.from_payload(
                    json.loads(row["payload"])
                )
                for alternative in assessment.alternatives:
                    pair = load_reviewed_thesis(reader, alternative.thesis_id)
                    if (
                        pair.document.input_snapshot.ticker,
                        pair.core_sha256,
                        pair.review.review_id,
                    ) != (
                        alternative.ticker,
                        alternative.thesis_core_sha256,
                        alternative.thesis_review_id,
                    ):
                        raise ValueError("CAA differs from its exact Reviewed Thesis references")
            except ValueError as error:
                blocked.append(
                    f"unreadable selected CAA {row['capital_allocation_assessment_id']}: {error}"
                )
        ledger_before = load_ledger_document(reader)
        replay_before = (
            None
            if ledger_before is None
            else replay_events_through(ledger_before.events, ledger_before.as_of)
        )
        report = CutoverReport(
            22,
            APPLICATION_SCHEMA_VERSION,
            {table: len(rows) for table, rows in copied.items()},
            len(handoffs),
            tuple(blocked),
        )
        if check or blocked:
            return report
        output.parent.mkdir(parents=True, exist_ok=True)
        # Exclusive creation closes the check/create race without overwriting a file.
        with output.open("xb"):
            pass
        try:
            with closing(sqlite3.connect(output)) as writer:
                writer.row_factory = sqlite3.Row
                writer.execute("PRAGMA foreign_keys=ON")
                writer.executescript(SCHEMA_SQL)
                writer.execute(f"PRAGMA user_version={APPLICATION_SCHEMA_VERSION}")
                writer.execute("BEGIN IMMEDIATE")
                writer.execute("PRAGMA defer_foreign_keys=ON")
                for table, rows in copied.items():
                    expected_columns = tuple(
                        info[1] for info in writer.execute(f'PRAGMA table_info("{table}")')
                    )  # nosec B608
                    for row in rows:
                        if set(row) != set(expected_columns):
                            raise ValueError(
                                f"source columns differ from the current table: {table}"
                            )
                        columns = ",".join(f'"{key}"' for key in expected_columns)
                        placeholders = ",".join("?" for _ in expected_columns)
                        # Identifiers come from the fixed destination schema; values are bound.
                        writer.execute(
                            f'INSERT INTO "{table}" ({columns}) VALUES ({placeholders})',  # nosec B608
                            tuple(row[key] for key in expected_columns),
                        )
                if writer.execute("PRAGMA foreign_key_check").fetchall():
                    raise ValueError("output foreign key check failed")
                writer.commit()
                if writer.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                    raise ValueError("output integrity check failed")
                for row in copied["thesis"]:
                    load_reviewed_thesis(writer, row["thesis_id"])
                ledger_after = load_ledger_document(writer)
                replay_after = (
                    None
                    if ledger_after is None
                    else replay_events_through(ledger_after.events, ledger_after.as_of)
                )
                if ledger_after != ledger_before or replay_after != replay_before:
                    raise ValueError("ledger events/replay differ after cutover")
                for table, rows in copied.items():
                    actual = [
                        dict(row)
                        for row in writer.execute(f'SELECT * FROM "{table}" ORDER BY rowid')  # nosec B608
                    ]  # nosec B608
                    if actual != rows:
                        raise ValueError(f"copied payload/identity differs: {table}")
            reader.rollback()
            reader.execute("BEGIN")
            if _snapshot(reader) != snapshot:
                raise ValueError("source changed after copy; stop writers and repeat cutover")
            handoff_dir = output.parent / (output.name + ".handoff")
            handoff_dir.mkdir()
            write_text_atomic(
                handoff_dir / "handoff.json",
                json.dumps(handoffs, ensure_ascii=False, indent=2) + "\n",
            )
            for identifier, paths in workspaces.items():
                for index, path in enumerate(paths):
                    shutil.copytree(path, handoff_dir / f"{identifier}-{index + 1}", symlinks=True)
        except BaseException:
            if (output.parent / (output.name + ".handoff")).exists():
                shutil.rmtree(output.parent / (output.name + ".handoff"))
            output.unlink(missing_ok=True)
            raise
        return report


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--selection", required=True, type=Path)
    parser.add_argument("--check", action="store_true")
    parser.add_argument(
        "--workspace-root",
        type=Path,
        help="explicit root containing bound active workspace manifests",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    report = cutover(
        source=args.source,
        output=args.output,
        selection=CutoverSelection.model_validate_json(args.selection.read_text()),
        check=args.check,
        workspace_root=args.workspace_root,
    )
    print(json.dumps(asdict(report), ensure_ascii=False))
    return 3 if report.blocked else 0


if __name__ == "__main__":
    raise SystemExit(main())
