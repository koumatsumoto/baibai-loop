"""application22と旧run cacheを保持し、最終世代へ一回だけ切り替える工具。

全application/run writerとactive draft編集を止め、原本backupを確認して実行する。
新評価・提案・Operationは空、runは通常Screeningによる再生成待ちで開始する。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sqlite3
from contextlib import ExitStack, closing
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Annotated, Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from baibai_engine.foundation.filesystem import write_text_atomic
from baibai_engine.foundation.yaml_io import safe_load
from baibai_engine.position.ledger import replay_events_through
from baibai_engine.position.ledger_read import load_ledger_document
from baibai_engine.screening.run_store.schema import RUN_STORE_SCHEMA_VERSION
from baibai_engine.screening.run_store.store import initialize_run_store
from baibai_engine.storage.application.json import canonical_json
from baibai_engine.storage.application.schema import APPLICATION_SCHEMA_VERSION, SCHEMA_SQL
from tools.cutover.assessment_proposal_v1 import (
    assessment_source_draft,
    legacy_caa,
    position_reason_source,
    retained_buy_binding,
    retained_ledger_buy_binding,
    verify_alternative_source,
)

NonEmptyStr = Annotated[str, Field(min_length=1, pattern=r"\S")]
_UNCHANGED = (
    "ledger_event",
    "ledger_meta",
    "ledger_market_price",
    "task",
    "market_context_assessment",
    "market_context_assessment_head",
    "portfolio_outcome",
)
_OLD_RESEARCH = (
    "thesis",
    "thesis_review",
    "capital_allocation_assessment",
    "position_review",
    "operation_session",
    "research_triage",
)
_NEW_RESEARCH = (
    "stock_assessment",
    "stock_assessment_review",
    "investment_proposal",
    "investment_proposal_item",
    "operation_session",
)


class RequiredRunSource(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)
    run_revision_id: NonEmptyStr
    ticker: Annotated[str, Field(pattern=r"^[0-9A-Z]{4}$")]


class KeepManifest(BaseModel):
    """Old source identities only; no new publication or Operation imports."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)
    schema_version: Literal[1] = 1
    keep_assessment_sources: tuple[NonEmptyStr, ...] = ()
    keep_proposal_sources: tuple[NonEmptyStr, ...] = ()
    keep_position_reason_sources: tuple[NonEmptyStr, ...] = ()
    retained_buy_references: tuple[NonEmptyStr, ...] = ()
    required_run_sources: tuple[RequiredRunSource, ...] = ()

    @field_validator("schema_version", mode="before")
    @classmethod
    def _version(cls, value: object) -> object:
        if type(value) is not int:
            raise ValueError("manifest schema_version must be an integer")
        return value

    @field_validator(
        "keep_assessment_sources",
        "keep_proposal_sources",
        "keep_position_reason_sources",
        "retained_buy_references",
        "required_run_sources",
        mode="before",
    )
    @classmethod
    def _tuples(cls, value: object) -> object:
        return tuple(value) if isinstance(value, list) else value

    @model_validator(mode="after")
    def _unique_sources(self) -> KeepManifest:
        for values in (
            self.keep_assessment_sources,
            self.keep_proposal_sources,
            self.keep_position_reason_sources,
            self.retained_buy_references,
        ):
            if len(set(values)) != len(values):
                raise ValueError("manifest source IDs must be unique")
        runs = [(item.run_revision_id, item.ticker) for item in self.required_run_sources]
        if len(set(runs)) != len(runs):
            raise ValueError("manifest run sources must be unique")
        return self


@dataclass(frozen=True)
class CutoverReport:
    source_version: int
    output_version: int
    source_run_version: int | None
    output_run_version: int | None
    copied_counts: dict[str, int]
    retained_buy_references: tuple[str, ...]
    draft_count: int
    active_handoffs: int
    research_recreation_required: bool
    run_recreation_required: bool
    material_limitations: tuple[str, ...]
    blocked: tuple[str, ...]


def _read_only(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only=ON")
    connection.execute("BEGIN")
    return connection


def _snapshot(source: sqlite3.Connection) -> dict[str, list[dict[str, Any]]]:
    if source.execute("PRAGMA user_version").fetchone()[0] != 22:
        raise ValueError(
            "domain cutover source must be application schema22; "
            "development schema23 is not a production migration source"
        )
    tables = {
        str(row[0])
        for row in source.execute(
            "SELECT name FROM sqlite_schema WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        )
    }
    expected = set(_UNCHANGED + _OLD_RESEARCH)
    if tables != expected:
        raise ValueError(
            f"source22 table set differs: missing={sorted(expected - tables)}, "
            f"unknown={sorted(tables - expected)}"
        )
    return {
        # Identifiers come only from the fixed source22 table contract.
        table: [
            dict(row)
            for row in source.execute(f'SELECT * FROM "{table}" ORDER BY rowid')  # nosec B608
        ]
        for table in _UNCHANGED + _OLD_RESEARCH
    }  # nosec B608


def _old_run_fingerprint(connection: sqlite3.Connection) -> str:
    if connection.execute("PRAGMA user_version").fetchone()[0] != 5:
        raise ValueError("domain cutover run source must be the retained schema5 cache")
    digest = hashlib.sha256()
    for table, order in (
        ("screening_run", "run_revision_id"),
        ("security_analysis", "run_revision_id,ordinal"),
        ("review_set", "review_set_id"),
    ):
        digest.update(table.encode())
        for row in connection.execute(f'SELECT * FROM "{table}" ORDER BY {order}'):  # nosec B608
            digest.update(canonical_json(dict(row)).encode())
            digest.update(b"\n")
    return digest.hexdigest()


def _run_sources(
    connection: sqlite3.Connection | None, requested: tuple[RequiredRunSource, ...]
) -> tuple[list[dict[str, Any]], list[str]]:
    if requested and connection is None:
        return [], [
            (
                "required old run sources need --source-runs; "
                "new runs cannot replace missing old evidence"
            )
        ]
    drafts: list[dict[str, Any]] = []
    blocked: list[str] = []
    if connection is None:
        return drafts, blocked
    for item in requested:
        header = connection.execute(
            "SELECT * FROM screening_run WHERE run_revision_id=?", (item.run_revision_id,)
        ).fetchone()
        row = connection.execute(
            "SELECT * FROM security_analysis WHERE run_revision_id=? AND ticker=?",
            (item.run_revision_id, item.ticker),
        ).fetchone()
        if header is None or row is None:
            blocked.append(
                f"missing required old run evidence: {item.run_revision_id}/{item.ticker}"
            )
            continue
        try:
            _validate_old_run_source(connection, dict(header), dict(row))
        except (ValueError, TypeError, KeyError) as error:
            blocked.append(f"old run evidence identity differs: {error}")
            continue
        raw = json.loads(str(row["payload"]))
        drafts.append(
            {
                "kind": "retained_analysis_source_draft",
                "source_run_revision_id": item.run_revision_id,
                "ticker": item.ticker,
                "source_header": dict(header),
                "source_analysis": raw,
                "source_analysis_payload": row["payload"],
                "new_run_required": True,
            }
        )
    return drafts, blocked


def _validate_old_run_source(
    connection: sqlite3.Connection, header: dict[str, Any], row: dict[str, Any]
) -> None:
    raw_header = json.loads(header["payload"])
    raw_analysis = json.loads(row["payload"])
    if not isinstance(raw_header, dict) or not isinstance(raw_analysis, dict):
        raise ValueError("old run header and analysis must be objects")
    for raw_key, column in (
        ("run_id", "public_run_id"),
        ("run_date", "run_date"),
        ("asof_date", "asof_date"),
        ("universe_size", "universe_size"),
    ):
        if raw_header.get(raw_key) != header[column]:
            raise ValueError(f"old run {raw_key} differs from SQL header")
    if (
        header["run_date"] != header["asof_date"]
        or header["public_run_id"] != f"screening-{header['asof_date'].replace('-', '')}"
        or datetime.fromisoformat(str(raw_header.get("run_at")))
        != datetime.fromisoformat(str(header["run_at"]))
        or datetime.fromisoformat(str(header["run_at"])).utcoffset() is None
        or row["run_revision_id"] != header["run_revision_id"]
        or raw_analysis.get("ticker") != row["ticker"]
        or not isinstance(raw_analysis.get("name"), str)
    ):
        raise ValueError("old run/analysis reference or time differs")
    revision = raw_header.get("run_revision_id", header["run_revision_id"])
    if revision != header["run_revision_id"]:
        raise ValueError("old run embedded revision differs from its source")
    rules_hash = raw_header.get("screening_rules_hash")
    if rules_hash is not None and (not isinstance(rules_hash, str) or not rules_hash.strip()):
        raise ValueError("old run rules identity is invalid")
    for reference in connection.execute(
        "SELECT review_set_id,run_revision_id,created_at,payload FROM review_set "
        "WHERE run_revision_id=?",
        (header["run_revision_id"],),
    ):
        raw = json.loads(reference["payload"])
        if not isinstance(raw, dict) or (
            raw.get("review_set_id") != reference["review_set_id"]
            or raw.get("run_revision_id") != header["run_revision_id"]
            or raw.get("as_of") != header["asof_date"]
            or raw.get("screening_rules_hash") != rules_hash
            or datetime.fromisoformat(str(raw.get("created_at")))
            != datetime.fromisoformat(str(reference["created_at"]))
        ):
            raise ValueError("old run Review Set reference/rules identity differs")


def _material_plan(
    snapshot: dict[str, list[dict[str, Any]]], manifest: KeepManifest
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[str]]:
    drafts: list[dict[str, Any]] = []
    bindings: list[dict[str, Any]] = []
    blocked: list[str] = []
    assessments = set(manifest.keep_assessment_sources)
    caas = {
        row["capital_allocation_assessment_id"]: row
        for row in snapshot["capital_allocation_assessment"]
    }
    positions = {row["position_review_id"]: row for row in snapshot["position_review"]}
    for identifier in manifest.keep_proposal_sources:
        row = caas.get(identifier)
        try:
            if row is not None:
                raw = legacy_caa(row)
                for alternative in raw["alternatives"]:
                    verify_alternative_source(snapshot, alternative)
                assessments.update(item["thesis_id"] for item in raw["alternatives"])
            elif identifier in positions:
                source = position_reason_source(snapshot, identifier)
                raw = source["source_document"]
                assessments.add(source["source_thesis_id"])
                if source["source_candidate_thesis_id"] is not None:
                    assessments.add(source["source_candidate_thesis_id"])
            else:
                raise ValueError(f"missing old proposal source: {identifier}")
            drafts.append(
                {
                    "kind": "proposal_source_draft",
                    "source_id": identifier,
                    "source_document": raw,
                    "saved_proposal_terms": None,
                    "new_proposal_required": True,
                }
            )
        except (ValueError, TypeError, KeyError) as error:
            blocked.append(str(error))
    for identifier in manifest.keep_position_reason_sources:
        try:
            source = position_reason_source(snapshot, identifier)
            drafts.append(source)
            assessments.add(source["source_thesis_id"])
            if source["source_candidate_thesis_id"] is not None:
                assessments.add(source["source_candidate_thesis_id"])
        except (ValueError, TypeError, KeyError) as error:
            blocked.append(str(error))
    for identifier in sorted(assessments):
        try:
            drafts.append(assessment_source_draft(snapshot, identifier))
        except (ValueError, TypeError, KeyError) as error:
            blocked.append(str(error))
    references = set(manifest.retained_buy_references)
    ledger_refs: dict[str, list[dict[str, Any]]] = {}
    for row in snapshot["ledger_event"]:
        raw = json.loads(row["payload"])
        reference = raw.get("decision_reference")
        side = raw.get("side", "buy" if raw.get("type") == "reservation" else None)
        if raw.get("type") in {"execution", "reservation"} and side == "buy" and reference:
            references.add(reference)
            ledger_refs.setdefault(reference, []).append(row)
    for identifier in sorted(references):
        try:
            row = caas.get(identifier)
            if identifier in ledger_refs:
                binding = retained_ledger_buy_binding(identifier, ledger_refs[identifier], row)
            elif row is None:
                raise ValueError(
                    f"missing original old buy reference: {identifier}; "
                    "no ticker is inferred from a Fill"
                )
            else:
                binding = retained_buy_binding(row)
            bindings.append(binding)
        except (ValueError, TypeError, KeyError) as error:
            blocked.append(str(error))
    return drafts, bindings, blocked


def _handoffs(snapshot: dict[str, list[dict[str, Any]]]) -> list[dict[str, Any]]:
    handoffs = []
    for row in snapshot["operation_session"]:
        if row["status"] != "active":
            continue
        payload = json.loads(row["payload"])
        artifacts = payload.get("artifacts", [])
        selection = next(
            (
                item
                for item in artifacts
                if isinstance(item, dict) and item.get("kind") == "research_selection"
            ),
            {},
        )
        targets = selection.get("targets", [])
        requests = [
            {
                "scope": "review_positions" if purpose == "holding" else None,
                "purpose": purpose,
                "targets": [target for target in targets if target.get("purpose") == purpose],
            }
            for purpose in ("new", "holding")
            if any(target.get("purpose") == purpose for target in targets)
        ]
        handoffs.append(
            {
                "source_operation_id": row["operation_id"],
                "started_at": row["started_at"],
                "request": selection.get("request"),
                "requests": requests,
                "source_payload": payload,
                "requires_reselection": True,
            }
        )
    return handoffs


def _selection_sources(
    snapshot: dict[str, list[dict[str, Any]]], handoffs: list[dict[str, Any]]
) -> tuple[list[dict[str, Any]], list[str]]:
    """Save selected old analysis values before retiring their cache or Triage."""
    drafts: list[dict[str, Any]] = []
    blocked: list[str] = []
    triages = {row["research_triage_id"]: row for row in snapshot["research_triage"]}
    for handoff in handoffs:
        selections = [
            item
            for item in handoff["source_payload"].get("artifacts", [])
            if isinstance(item, dict) and item.get("kind") == "research_selection"
        ]
        for selection in selections:
            tickers = {item["ticker"] for item in selection.get("targets", [])}
            explicit = selection.get("explicit_run")
            triage_id = selection.get("research_triage_id")
            if isinstance(explicit, dict):
                analyses = explicit.get("analyses")
                if not isinstance(analyses, dict) or not tickers <= analyses.keys():
                    blocked.append(
                        f"old selected analysis snapshot is incomplete: "
                        f"{handoff['source_operation_id']}"
                    )
                    continue
                if any(
                    not isinstance(analyses[ticker], dict)
                    or analyses[ticker].get("ticker") != ticker
                    for ticker in tickers
                ):
                    blocked.append("old selected analysis snapshot ticker differs")
                    continue
                drafts.append(
                    {
                        "kind": "retained_selection_source_draft",
                        "source_operation_id": handoff["source_operation_id"],
                        "source_run_revision_id": explicit.get("run_revision_id"),
                        "source_as_of": explicit.get("as_of"),
                        "source_run_at": explicit.get("run_at"),
                        "source_run_metadata": explicit.get("run_metadata"),
                        "selected_analyses": {
                            ticker: analyses[ticker] for ticker in sorted(tickers)
                        },
                        "requires_reselection": True,
                    }
                )
            elif triage_id is not None:
                # Source22 admits only new targets through Triage. Holding
                # evidence stays in the source payload and preserved workspace.
                new_tickers = {
                    item["ticker"]
                    for item in selection.get("targets", [])
                    if item.get("purpose") == "new"
                }
                if not new_tickers:
                    continue
                row = triages.get(triage_id)
                if row is None:
                    blocked.append(f"missing old selected Triage evidence: {triage_id}")
                    continue
                raw = json.loads(row["payload"])
                if (
                    not isinstance(raw, dict)
                    or raw.get("research_triage_id") != triage_id
                    or raw.get("run_revision_id") != row["run_revision_id"]
                    or not isinstance(raw.get("entries"), list)
                ):
                    blocked.append(f"old selected Triage source identity differs: {triage_id}")
                    continue
                entries = {entry.get("ticker"): entry for entry in raw["entries"]}
                if not new_tickers <= entries.keys() or any(
                    not isinstance(entries[ticker].get("candidate_snapshot"), dict)
                    for ticker in new_tickers
                ):
                    blocked.append(f"old selected Triage analysis is incomplete: {triage_id}")
                    continue
                drafts.append(
                    {
                        "kind": "retained_selection_source_draft",
                        "source_operation_id": handoff["source_operation_id"],
                        "source_triage_id": triage_id,
                        "source_run_revision_id": row["run_revision_id"],
                        "source_as_of": row["as_of"],
                        "source_published_at": row["published_at"],
                        "selected_entries": [entries[ticker] for ticker in sorted(new_tickers)],
                        "requires_reselection": True,
                    }
                )
    return drafts, blocked


def _workspace_handoffs(
    *, root: Path | None, handoffs: list[dict[str, Any]], destinations: tuple[Path, ...]
) -> tuple[dict[str, list[Path]], list[str]]:
    if not handoffs:
        return {}, []
    found: dict[str, list[Path]] = {item["source_operation_id"]: [] for item in handoffs}
    if root is None or not root.is_dir():
        return found, ["active workspaces require an existing explicit --workspace-root"]
    root = root.resolve()
    blocked = []
    for manifest in root.rglob("manifest.yaml"):
        if not manifest.resolve().is_relative_to(root):
            blocked.append("workspace manifest points outside the supplied root")
            continue
        try:
            raw = safe_load(manifest.read_text(encoding="utf-8"))
        except (OSError, ValueError, yaml.YAMLError) as error:
            blocked.append(f"unreadable workspace manifest: {error}")
            continue
        if not isinstance(raw, dict):
            blocked.append("workspace manifest is not an object")
            continue
        identifier = raw.get("operation_id")
        if identifier in found:
            found[identifier].append(manifest.parent)
    for identifier, paths in found.items():
        if not paths:
            blocked.append(f"active workspace not found in supplied root: {identifier}")
            continue
        parents = [
            path
            for path in paths
            if not any(path != other and path.is_relative_to(other) for other in paths)
        ]
        found[identifier] = parents
        for path in parents:
            if any(destination.is_relative_to(path) for destination in destinations):
                blocked.append("output must be outside the source active workspace")
            for file in path.rglob("*"):
                if file.is_symlink() and not file.resolve().is_relative_to(path.resolve()):
                    blocked.append(f"active workspace points outside supplied root: {identifier}")
                if file.name == "manifest.yaml":
                    child = safe_load(file.read_text(encoding="utf-8"))
                    if not isinstance(child, dict) or child.get("operation_id") != identifier:
                        blocked.append(
                            f"nested workspace belongs to another Operation: {identifier}"
                        )
    return found, blocked


def _copy_unchanged(writer: sqlite3.Connection, snapshot: dict[str, list[dict[str, Any]]]) -> None:
    for table in _UNCHANGED:
        columns = tuple(info[1] for info in writer.execute(f'PRAGMA table_info("{table}")'))  # nosec B608
        for row in snapshot[table]:
            if set(row) != set(columns):
                raise ValueError(f"source columns differ from the final table: {table}")
            names = ",".join(f'"{column}"' for column in columns)
            placeholders = ",".join("?" for _ in columns)
            # Table/columns are fixed schema identifiers; all values are bound.
            writer.execute(
                f'INSERT INTO "{table}" ({names}) VALUES ({placeholders})',  # nosec B608
                tuple(row[column] for column in columns),
            )  # nosec B608


def _verify_copy(
    writer: sqlite3.Connection,
    snapshot: dict[str, list[dict[str, Any]]],
    ledger_before: object,
    replay_before: object,
) -> None:
    if writer.execute("PRAGMA foreign_key_check").fetchall():
        raise ValueError("output foreign key check failed")
    if writer.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
        raise ValueError("output integrity check failed")
    for table in _UNCHANGED:
        actual = [dict(row) for row in writer.execute(f'SELECT * FROM "{table}" ORDER BY rowid')]  # nosec B608
        if actual != snapshot[table]:
            raise ValueError(f"copied facts/payload differ: {table}")
    for table in _NEW_RESEARCH:
        if writer.execute(f'SELECT count(*) FROM "{table}"').fetchone()[0] != 0:  # nosec B608
            raise ValueError(f"new Research publications must be empty: {table}")
    ledger_after = load_ledger_document(writer)
    replay_after = (
        None
        if ledger_after is None
        else replay_events_through(ledger_after.events, ledger_after.as_of)
    )
    if ledger_after != ledger_before or replay_after != replay_before:
        raise ValueError("ledger facts/replay differ after cutover")


def _verify_source_unchanged(
    reader: sqlite3.Connection,
    snapshot: dict[str, list[dict[str, Any]]],
    run_reader: sqlite3.Connection | None,
    run_fingerprint: str | None,
) -> None:
    reader.rollback()
    reader.execute("BEGIN")
    if _snapshot(reader) != snapshot:
        raise ValueError("source changed after copy; stop writers and repeat cutover")
    if run_reader is not None:
        run_reader.rollback()
        run_reader.execute("BEGIN")
        if _old_run_fingerprint(run_reader) != run_fingerprint:
            raise ValueError("run source changed after copy; stop writers and repeat cutover")


def cutover(
    *,
    source_app: Path,
    output_app: Path,
    manifest: KeepManifest,
    draft_dir: Path,
    source_runs: Path | None = None,
    output_runs: Path | None = None,
    workspace_root: Path | None = None,
    check: bool = False,
) -> CutoverReport:
    source_app, output_app, draft_dir = (
        source_app.resolve(),
        output_app.resolve(),
        draft_dir.resolve(),
    )
    source_runs = None if source_runs is None else source_runs.resolve()
    output_runs = None if output_runs is None else output_runs.resolve()
    outputs = (output_app, draft_dir) + (() if output_runs is None else (output_runs,))
    sources = (source_app,) + (() if source_runs is None else (source_runs,))
    if (
        any(path.exists() for path in outputs)
        or len(set(outputs)) != len(outputs)
        or any(
            source == output or source.is_relative_to(draft_dir)
            for source in sources
            for output in outputs
        )
        or output_app.is_relative_to(draft_dir)
        or (output_runs is not None and output_runs.is_relative_to(draft_dir))
    ):
        raise ValueError("sources must differ from new, absent outputs and draft directory")
    with ExitStack() as stack:
        reader = stack.enter_context(closing(_read_only(source_app)))
        run_reader = (
            None if source_runs is None else stack.enter_context(closing(_read_only(source_runs)))
        )
        snapshot = _snapshot(reader)
        run_fingerprint = None if run_reader is None else _old_run_fingerprint(run_reader)
        drafts, bindings, blocked = _material_plan(snapshot, manifest)
        run_drafts, run_blocked = _run_sources(run_reader, manifest.required_run_sources)
        drafts.extend(run_drafts)
        blocked.extend(run_blocked)
        handoffs = _handoffs(snapshot)
        selection_drafts, selection_blocked = _selection_sources(snapshot, handoffs)
        drafts.extend(selection_drafts)
        blocked.extend(selection_blocked)
        workspaces, workspace_blocked = _workspace_handoffs(
            root=workspace_root, handoffs=handoffs, destinations=outputs
        )
        blocked.extend(workspace_blocked)
        ledger_before = load_ledger_document(reader)
        replay_before = (
            None
            if ledger_before is None
            else replay_events_through(ledger_before.events, ledger_before.as_of)
        )
        report = CutoverReport(
            22,
            APPLICATION_SCHEMA_VERSION,
            None if run_reader is None else 5,
            None if output_runs is None else RUN_STORE_SCHEMA_VERSION,
            {table: len(snapshot[table]) for table in _UNCHANGED},
            tuple(binding["reference"] for binding in bindings),
            len(drafts),
            len(handoffs),
            True,
            output_runs is not None,
            tuple(
                f"{draft.get('source_thesis_id', draft.get('source_id'))}: {limitation}"
                for draft in drafts
                for limitation in draft.get("source_validation_limits", [])
            ),
            tuple(blocked),
        )
        if check or blocked:
            return report
        created_files: list[Path] = []
        created_draft_dir = False
        try:
            output_app.parent.mkdir(parents=True, exist_ok=True)
            with output_app.open("xb"):
                pass
            created_files.append(output_app)
            with closing(sqlite3.connect(output_app)) as writer:
                writer.row_factory = sqlite3.Row
                writer.execute("PRAGMA foreign_keys=ON")
                writer.executescript(SCHEMA_SQL)
                writer.execute(f"PRAGMA user_version={APPLICATION_SCHEMA_VERSION}")
                writer.execute("BEGIN IMMEDIATE")
                writer.execute("PRAGMA defer_foreign_keys=ON")
                _copy_unchanged(writer, snapshot)
                for binding in bindings:
                    writer.execute(
                        "INSERT INTO position_decision_reference(reference,ticker,side,origin,"
                        "proposal_id,source_id,source_digest) VALUES (?,?,?,?,?,?,?)",
                        tuple(
                            binding[field]
                            for field in (
                                "reference",
                                "ticker",
                                "side",
                                "origin",
                                "proposal_id",
                                "source_id",
                                "source_digest",
                            )
                        ),
                    )
                _verify_copy(writer, snapshot, ledger_before, replay_before)
                writer.commit()
            if output_runs is not None:
                output_runs.parent.mkdir(parents=True, exist_ok=True)
                with output_runs.open("xb"):
                    pass
                created_files.append(output_runs)
                initialize_run_store(output_runs)
            _verify_source_unchanged(reader, snapshot, run_reader, run_fingerprint)
            draft_dir.mkdir(parents=True)
            created_draft_dir = True
            for index, draft in enumerate(drafts, 1):
                write_text_atomic(
                    draft_dir / f"material-{index:04d}.json",
                    json.dumps(draft, ensure_ascii=False, indent=2) + "\n",
                )
            write_text_atomic(
                draft_dir / "handoff.json",
                json.dumps(handoffs, ensure_ascii=False, indent=2) + "\n",
            )
            write_text_atomic(
                draft_dir / "recreation.json",
                json.dumps(
                    {
                        "manifest": manifest.model_dump(mode="json"),
                        "report": asdict(report),
                        "next_steps": [
                            "通常Screeningで実時刻・新UUIDのrunを作る",
                            "通常selection-context/prepareで新Operationを作る",
                            "保存材料を確認して新Assessment/Reviewをpromoteする",
                            "そのOperationと現在入力から新Proposalを作る",
                        ],
                    },
                    ensure_ascii=False,
                    indent=2,
                )
                + "\n",
            )
            for identifier, paths in workspaces.items():
                directory = hashlib.sha256(identifier.encode()).hexdigest()[:16]
                for index, path in enumerate(paths, 1):
                    shutil.copytree(
                        path, draft_dir / f"workspace-{directory}-{index}", symlinks=True
                    )
            _verify_source_unchanged(reader, snapshot, run_reader, run_fingerprint)
        except BaseException:
            if created_draft_dir:
                shutil.rmtree(draft_dir)
            for path in created_files:
                path.unlink(missing_ok=True)
            raise
        return report


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-app", required=True, type=Path)
    parser.add_argument("--source-runs", type=Path)
    parser.add_argument("--output-app", required=True, type=Path)
    parser.add_argument("--output-runs", type=Path)
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--draft-dir", required=True, type=Path)
    parser.add_argument(
        "--workspace-root",
        type=Path,
        help="explicit root containing bound active workspace manifests",
    )
    parser.add_argument("--check", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    report = cutover(
        source_app=args.source_app,
        source_runs=args.source_runs,
        output_app=args.output_app,
        output_runs=args.output_runs,
        manifest=KeepManifest.model_validate_json(args.manifest.read_text(encoding="utf-8")),
        draft_dir=args.draft_dir,
        workspace_root=args.workspace_root,
        check=args.check,
    )
    print(json.dumps(asdict(report), ensure_ascii=False))
    return 3 if report.blocked else 0


if __name__ == "__main__":
    raise SystemExit(main())
