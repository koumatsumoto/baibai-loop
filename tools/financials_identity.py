"""schema 7/28から8/29への一回の隔離copy。取得・PUT・daily呼出しは持たない。"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import sqlite3
from collections import Counter
from contextlib import closing
from copy import deepcopy
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import TypeAdapter

from baibai_engine.investment_judgment.company_research.store import load_research
from baibai_engine.investment_judgment.judgment_retrospective.query import retrospective_context
from baibai_engine.investment_judgment.judgment_retrospective.store import load_retrospective
from baibai_engine.investment_judgment.stock_assessment.store import load_reviewed_assessment
from baibai_engine.investment_judgment.trade_proposal.contracts import ProposalResearchScope
from baibai_engine.investment_judgment.trade_proposal.query import (
    load_investment_proposal,
    proposal_records,
)
from baibai_engine.investment_judgment.workflow.operation.models import OperationPayload
from baibai_engine.investment_judgment.workflow.operation.progress import proposal_scope
from baibai_engine.investment_judgment.workflow.selection import (
    GeneratedResearchSelection,
    GeneratedResearchSelectionV4,
    parse_selection,
    selection_sha256,
)
from baibai_engine.investment_judgment.workflow.selection_evidence import read_selection_evidence
from baibai_engine.machine_analysis.generation.contracts import (
    GeneratedModel,
    GenerationMetadata,
    validate_full_plan,
    validate_model_scope,
)
from baibai_engine.machine_analysis.generation.query import read_research_model_inputs
from baibai_engine.machine_analysis.screening.contracts import ScreeningSelection
from baibai_engine.machine_analysis.screening.selection_query import read_publication
from baibai_engine.primitives.json import canonical_json
from baibai_engine.storage.sqlite.application.write import initialize_database
from baibai_engine.storage.sqlite.machine_analysis import initialize_run_store

OLD = "company_valuation"
NEW = "company_financials"
MIGRATION_ID = "company-financials-id-v1"
_POLICY = TypeAdapter(ScreeningSelection)


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _open(path: Path, version: int) -> sqlite3.Connection:
    connection = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only=ON")
    connection.execute("PRAGMA foreign_keys=ON")
    try:
        if connection.execute("PRAGMA user_version").fetchone()[0] != version:
            raise ValueError("cutover source schema differs from the exact supported version")
        if connection.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise ValueError("cutover database integrity failed")
        if connection.execute("PRAGMA foreign_key_check").fetchall():
            raise ValueError("cutover database foreign keys failed")
        connection.execute("BEGIN")
        return connection
    except BaseException:
        connection.close()
        raise


def _self_contained(path: Path) -> None:
    if path.is_symlink():
        raise ValueError("source SQLite snapshot must not be a file symlink")
    if any(Path(str(path) + suffix).exists() for suffix in ("-wal", "-shm", "-journal")):
        raise ValueError("source must be a sealed self-contained SQLite snapshot without sidecars")


def _tables(connection: sqlite3.Connection) -> list[str]:
    return [
        row[0]
        for row in connection.execute(
            "SELECT name FROM sqlite_schema WHERE type='table' AND name NOT LIKE 'sqlite_%' "
            "ORDER BY rowid"
        )
    ]


def _schema(connection: sqlite3.Connection) -> dict[str, tuple[str, str]]:
    return {
        row[0]: (row[1], " ".join(row[2].split()))
        for row in connection.execute(
            "SELECT name,type,sql FROM sqlite_schema WHERE sql IS NOT NULL "
            "AND name NOT LIKE 'sqlite_%'"
        )
    }


def _no_unknown_identity(value: Any) -> None:
    if isinstance(value, dict):
        for key, child in value.items():
            if key == OLD:
                raise ValueError("legacy model identity remains in an unknown dictionary key")
            if key in {"dataset", "ref"} and "source_tier" in value:
                continue  # 当時の原provenanceを型付きmodel IDへ読み替えない。
            _no_unknown_identity(child)
    elif isinstance(value, (list, tuple)):
        for child in value:
            _no_unknown_identity(child)
    elif value == OLD:
        raise ValueError("legacy model identity remains in an unknown field")


def _generation(value: dict[str, Any]) -> dict[str, Any]:
    result = deepcopy(value)
    for definition in (result.get("subject_plan") or {}).values():
        ids = [NEW if name == OLD else name for name in definition["model_ids"]]
        if len(ids) != len(set(ids)):
            raise ValueError("model plan identities collide after cutover")
        definition["model_ids"] = ids
    for model in result.get("models", []):
        if model["model_id"] == OLD:
            model["model_id"] = NEW
    return result


def _policy(value: dict[str, Any]) -> dict[str, Any]:
    decoded = _POLICY.dump_python(_POLICY.validate_python(value), mode="json")
    if canonical_json(decoded) != canonical_json(value):
        raise ValueError("unknown or noncanonical policy fields require a separate cutover")
    result = deepcopy(value)
    for card in result["result"].get("cards", []):
        revisions = card["model_revisions"]
        if OLD in revisions:
            if NEW in revisions:
                raise ValueError("candidate model revision keys collide")
            revisions[NEW] = revisions.pop(OLD)
        ids = [NEW if name == OLD else name for name in card["not_generated"]]
        if len(ids) != len(set(ids)):
            raise ValueError("candidate missing model identities collide")
        card["not_generated"] = ids
    _no_unknown_identity(result)
    _POLICY.validate_python(result)
    return result


def _selection(value: dict[str, Any]) -> tuple[str, str, dict[str, Any]]:
    before = parse_selection(value)
    original = canonical_json(before.model_dump(mode="json"))
    if original != canonical_json(value):
        raise ValueError("source selection differs from its canonical contract")
    result = deepcopy(value)
    if result["schema_version"] in (3, 4):
        result["inputs"]["generation"] = _generation(result["inputs"]["generation"])
        if result["inputs"]["screening_selection"] is not None:
            result["inputs"]["screening_selection"] = _policy(
                result["inputs"]["screening_selection"]
            )
    _no_unknown_identity(result)
    # ModelInputSliceの既存validatorが(model_id, subject)で正準化する。
    after = parse_selection(result)
    return original, selection_sha256(after), after.model_dump(mode="json")


def _validated_publications(
    connection: sqlite3.Connection,
) -> dict[str, list[tuple[str, ProposalResearchScope]]]:
    scopes: dict[str, list[tuple[str, ProposalResearchScope]]] = {}
    for (identity,) in connection.execute("SELECT proposal_id FROM investment_proposal"):
        proposal = load_investment_proposal(connection, identity)
        proposal_records(connection, identity)
        scope = proposal.research_scope
        scopes.setdefault(scope.operation_id, []).append((identity, scope))
    for (identity,) in connection.execute("SELECT research_id FROM company_research"):
        load_research(connection, identity)
    for (identity,) in connection.execute("SELECT assessment_id FROM stock_assessment"):
        load_reviewed_assessment(connection, identity)
    for (identity,) in connection.execute("SELECT retrospective_id FROM judgment_retrospective"):
        retrospective_context(connection, load_retrospective(connection, identity))
    return scopes


def _validate_run_models(connection: sqlite3.Connection) -> None:
    metadata = {
        row[0]: GenerationMetadata.model_validate_json(row[1])
        for row in connection.execute("SELECT generation_id,header FROM model_generation")
    }
    for generation_id, header in metadata.items():
        if header.generation_id != generation_id:
            raise ValueError("generation column differs from its saved identity")
        validate_full_plan(header)
    for row in connection.execute("SELECT * FROM generated_model"):
        payload = json.loads(gzip.decompress(row["payload_gzip"]))
        _no_unknown_identity(payload)
        model = GeneratedModel(
            model_id=row["model_id"],
            subject=row["subject"],
            calculation_revision=row["calculation_revision"],
            payload=payload,
        )
        validate_model_scope(metadata[row["generation_id"]], model)


def _operation(
    row: sqlite3.Row,
    scopes: dict[str, list[tuple[str, ProposalResearchScope]]],
    recorded_at: str,
) -> tuple[str, tuple[str, ...] | None]:
    payload = json.loads(row["payload"])
    selections = [item for item in payload["artifacts"] if item.get("kind") == "research_selection"]
    if len(selections) != 1:
        raise ValueError("cutover requires exactly one fixed selection per Operation")
    original, normalized_hash, normalized = _selection(selections[0])
    original_hash = hashlib.sha256(original.encode()).hexdigest()
    normalized_artifacts = [
        normalized if item.get("kind") == "research_selection" else item
        for item in payload["artifacts"]
    ]
    _no_unknown_identity({**payload, "artifacts": normalized_artifacts})
    if original_hash == normalized_hash:
        return str(row["payload"]), None
    if row["status"] != "completed":
        raise ValueError("active Operation prevents this cutover")
    references = scopes.get(row["operation_id"], [])
    if not references:
        raise ValueError(
            "changed Selection without a published Proposal requires a separate design"
        )
    expected = proposal_scope(row["operation_id"], OperationPayload.model_validate(payload))
    if any(scope != expected for _, scope in references):
        raise ValueError("original Proposal scope differs from the fixed Operation")
    payload["artifacts"] = normalized_artifacts
    return canonical_json(payload), (
        str(row["operation_id"]),
        MIGRATION_ID,
        recorded_at,
        original_hash,
        normalized_hash,
        original,
    )


def _row(
    table: str,
    source: sqlite3.Row,
    scopes: dict[str, list[tuple[str, ProposalResearchScope]]],
    recorded_at: str,
) -> tuple[tuple[Any, ...], tuple[str, ...] | None]:
    values = dict(source)
    receipt = None
    if table == "model_generation":
        header = json.loads(values["header"])
        metadata = GenerationMetadata.model_validate(header)
        if canonical_json(metadata.model_dump(mode="json")) != canonical_json(header):
            raise ValueError("unknown or noncanonical generation header")
        changed = _generation(header)
        _no_unknown_identity(changed)
        values["header"] = canonical_json(
            GenerationMetadata.model_validate(changed).model_dump(mode="json")
        )
    elif table == "generated_model":
        if values["model_id"] == OLD:
            values["model_id"] = NEW
    elif table == "screening_selection":
        before = gzip.decompress(values["payload_gzip"])
        after = canonical_json(_policy(json.loads(before))).encode()
        if before != after:
            values["payload_gzip"] = gzip.compress(after, mtime=0)
    elif table == "operation_session":
        values["payload"], receipt = _operation(source, scopes, recorded_at)
    elif "payload" in values:
        _no_unknown_identity(json.loads(values["payload"]))
    return tuple(values.values()), receipt


def _copy(
    source: sqlite3.Connection,
    destination: sqlite3.Connection,
    scopes: dict[str, list[tuple[str, ProposalResearchScope]]],
    recorded_at: str,
) -> Counter[str]:
    expected_schema = _schema(source)
    current_schema = {
        key: value
        for key, value in _schema(destination).items()
        if not key.startswith("selection_identity_migration")
    }
    if expected_schema != current_schema:
        raise ValueError("unknown source schema objects require a separate cutover")
    counts: Counter[str] = Counter()
    for table in _tables(source):
        counts[table] = 0
        columns = [row[1] for row in source.execute(f'PRAGMA table_info("{table}")')]
        placeholders = ",".join("?" for _ in columns)
        # The exact schema comparison above fixes every identifier; values remain bound.
        query = f'SELECT * FROM "{table}"'  # nosec B608
        if table not in {"generated_model", "stock_analysis"}:
            query += " ORDER BY rowid"
        insert = f'INSERT INTO "{table}" VALUES ({placeholders})'  # nosec B608
        for original in source.execute(query):
            values, receipt = _row(table, original, scopes, recorded_at)
            destination.execute(insert, values)
            counts[table] += 1
            if receipt is not None:
                destination.execute(
                    "INSERT INTO selection_identity_migration VALUES (?,?,?,?,?,?)", receipt
                )
                counts["selection_identity_migration"] += 1
    return counts


def _verify_rows(
    source: sqlite3.Connection,
    destination: sqlite3.Connection,
    scopes: dict[str, list[tuple[str, ProposalResearchScope]]],
    recorded_at: str,
) -> None:
    target_schema = {
        key: value
        for key, value in _schema(destination).items()
        if not key.startswith("selection_identity_migration")
    }
    if _schema(source) != target_schema:
        raise ValueError("copy schema differs from the exact cutover contract")
    for table in _tables(source):
        info = source.execute(f'PRAGMA table_info("{table}")').fetchall()
        keys = [column[1] for column in sorted(info, key=lambda item: item[5]) if column[5]]
        names = [column[1] for column in info]
        where = " AND ".join(f'"{key}"=?' for key in keys)
        count = 0
        # Identifiers come from the exact schema comparison, never command arguments.
        query = f'SELECT * FROM "{table}"'  # nosec B608
        lookup = f'SELECT * FROM "{table}" WHERE {where}'  # nosec B608
        count_query = f'SELECT count(*) FROM "{table}"'  # nosec B608
        for row in source.execute(query):
            expected, receipt = _row(table, row, scopes, recorded_at)
            actual = destination.execute(
                lookup,
                tuple(expected[names.index(key)] for key in keys),
            ).fetchone()
            if actual is None or tuple(actual) != expected:
                raise ValueError("copy differs beyond the permitted identity transformation")
            if table == "operation_session":
                evidence = destination.execute(
                    "SELECT * FROM selection_identity_migration WHERE operation_id=?",
                    (row["operation_id"],),
                ).fetchone()
                if (None if evidence is None else tuple(evidence)) != receipt:
                    raise ValueError("copy evidence differs from the exact source Selection")
            count += 1
        if destination.execute(count_query).fetchone()[0] != count:
            raise ValueError("copy contains added or missing rows")


def _verify_evidence(app: Path, runs: Path) -> dict[str, int]:
    counts: Counter[str] = Counter()
    with closing(_open(app, 29)) as connection:
        scopes = _validated_publications(connection)
        for receipt in connection.execute("SELECT * FROM selection_identity_migration"):
            identity = receipt["operation_id"]
            for proposal_id, scope in scopes[identity]:
                evidence = read_selection_evidence(
                    operation_id=identity,
                    sha256=scope.selection_sha256,
                    proposal_id=proposal_id,
                    db_path=app,
                )
                if evidence["canonical_json"] != receipt["published_selection_canonical"]:
                    raise ValueError("publication evidence differs from its original bytes")
                counts["published_evidence"] += 1
            raw = json.loads(
                connection.execute(
                    "SELECT payload FROM operation_session WHERE operation_id=?", (identity,)
                ).fetchone()[0]
            )
            current = next(a for a in raw["artifacts"] if a.get("kind") == "research_selection")
            original = json.loads(receipt["published_selection_canonical"])
            old_generation = original["inputs"]["generation"]
            new_generation = current["inputs"]["generation"]
            # 旧indexは旧canonicalへ解決し、同じpayloadを新indexへ対応付ける。
            for index, model in enumerate(old_generation["models"]):
                target = NEW if model["model_id"] == OLD else model["model_id"]
                matches = [
                    m
                    for m in new_generation["models"]
                    if m["model_id"] == target and m["subject"] == model["subject"]
                ]
                if len(matches) != 1 or canonical_json(matches[0]["payload"]) != canonical_json(
                    old_generation["models"][index]["payload"]
                ):
                    raise ValueError("original model index no longer resolves its exact payload")
                counts["original_index_payloads"] += 1
            parsed = parse_selection(current)
            if not isinstance(parsed, (GeneratedResearchSelection, GeneratedResearchSelectionV4)):
                raise ValueError("migration evidence requires a generated Selection")
            saved = parsed.inputs
            canonical = read_research_model_inputs(
                saved.generation.generation_id,
                targets=[t["ticker"] for t in current["targets"]],
                path=runs,
            )
            if canonical is not None and canonical != saved.generation:
                raise ValueError("normalized input slice differs from retained generation")
            counts[
                "retained_generation_matches"
                if canonical is not None
                else "generation_not_retained"
            ] += 1
            if saved.screening_selection_id is not None:
                publication = read_publication(path=runs, selection_id=saved.screening_selection_id)
                if publication is not None and (
                    publication.selection != saved.screening_selection
                    or publication.published_at != saved.screening_published_at
                ):
                    raise ValueError("normalized policy copy differs from its retained publication")
                counts[
                    "retained_policy_matches" if publication is not None else "policy_not_retained"
                ] += 1
        counts["human_reports_verified"] = connection.execute(
            "SELECT count(*) FROM proposal_human_report"
        ).fetchone()[0]
    return dict(counts)


def prepare_copy(
    *,
    source_app: Path,
    source_runs: Path,
    output: Path,
    recorded_at: datetime,
) -> dict[str, Any]:
    """原DBを変更せず、限定変換と独立読取検証済みの2DBを作る。"""
    if recorded_at.utcoffset() is None:
        raise ValueError("recorded_at must be timezone aware")
    if recorded_at > datetime.now(UTC):
        raise ValueError("recorded_at cannot be in the future")
    for source in (source_app, source_runs):
        _self_contained(source)
    recorded = recorded_at.astimezone(UTC).isoformat()
    source_hashes = {"application": file_sha256(source_app), "runs": file_sha256(source_runs)}
    manifest_path = output / "manifest.json"
    if output.exists():
        if not manifest_path.is_file():
            raise ValueError("existing output is incomplete; it will not be overwritten")
        existing: dict[str, Any] = json.loads(manifest_path.read_text())
        if (
            set(existing)
            != {
                "migration_id",
                "recorded_at",
                "source_sha256",
                "output_sha256",
                "schemas",
                "rows",
                "verification",
            }
            or existing["migration_id"] != MIGRATION_ID
            or existing["schemas"] != {"application": 29, "runs": 8}
        ):
            raise ValueError("existing manifest differs from this cutover contract")
        if existing["source_sha256"] != source_hashes or existing["recorded_at"] != recorded:
            raise ValueError("existing output belongs to another source or invocation")
        for name, key in (("application", "application.sqlite"), ("runs", "runs.sqlite")):
            if file_sha256(output / key) != existing["output_sha256"][name]:
                raise ValueError("existing cutover output changed")
        with (
            closing(_open(source_app, 28)) as a,
            closing(_open(source_runs, 7)) as r,
            closing(_open(output / "application.sqlite", 29)) as a_new,
            closing(_open(output / "runs.sqlite", 8)) as r_new,
        ):
            scopes = _validated_publications(a)
            _validate_run_models(r)
            _verify_rows(a, a_new, scopes, recorded)
            _verify_rows(r, r_new, scopes, recorded)
        if (
            _verify_evidence(output / "application.sqlite", output / "runs.sqlite")
            != existing["verification"]
        ):
            raise ValueError("existing verification differs from the current readers")
        for source in (source_app, source_runs):
            _self_contained(source)
        if {
            "application": file_sha256(source_app),
            "runs": file_sha256(source_runs),
        } != source_hashes:
            raise ValueError("source databases changed during preparation")
        return existing
    with closing(_open(source_app, 28)) as app, closing(_open(source_runs, 7)) as runs:
        if app.execute("SELECT count(*) FROM operation_session WHERE status='active'").fetchone()[
            0
        ]:
            raise ValueError("active Operation prevents this cutover")
        if (
            runs.execute(
                "SELECT count(*) FROM generated_model WHERE model_id=?", (OLD,)
            ).fetchone()[0]
            == 0
        ):
            raise ValueError("source contains no legacy model identities")
        if runs.execute(
            "SELECT 1 FROM generated_model a JOIN generated_model b "
            "ON a.generation_id=b.generation_id AND a.subject=b.subject "
            "WHERE a.model_id=? AND b.model_id=? LIMIT 1",
            (OLD, NEW),
        ).fetchone():
            raise ValueError("generated model identities collide after cutover")
        scopes = _validated_publications(app)
        _validate_run_models(runs)
        # 型付き改名と公開hash照合を作成前に行う。
        for row in app.execute("SELECT * FROM operation_session"):
            _operation(row, scopes, recorded)
        output.mkdir(parents=True, mode=0o700)
        app_target = output / "application.sqlite"
        runs_target = output / "runs.sqlite"
        initialize_database(app_target)
        initialize_run_store(runs_target)
        with closing(sqlite3.connect(app_target)) as a, closing(sqlite3.connect(runs_target)) as r:
            for c in (a, r):
                c.row_factory = sqlite3.Row
                c.execute("PRAGMA foreign_keys=ON")
            counts = {
                "runs": dict(_copy(runs, r, scopes, recorded)),
                "application": dict(_copy(app, a, scopes, recorded)),
            }
            r.commit()
            a.commit()
            _verify_rows(runs, r, scopes, recorded)
            _verify_rows(app, a, scopes, recorded)
    verification = _verify_evidence(app_target, runs_target)
    for source in (source_app, source_runs):
        _self_contained(source)
    if {"application": file_sha256(source_app), "runs": file_sha256(source_runs)} != source_hashes:
        raise ValueError("source databases changed during preparation")
    manifest = {
        "migration_id": MIGRATION_ID,
        "recorded_at": recorded,
        "source_sha256": source_hashes,
        "output_sha256": {"application": file_sha256(app_target), "runs": file_sha256(runs_target)},
        "schemas": {"application": 29, "runs": 8},
        "rows": counts,
        "verification": verification,
    }
    with manifest_path.open("x", encoding="utf-8") as manifest_file:
        manifest_file.write(json.dumps(manifest, ensure_ascii=False, indent=2))
    return manifest


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-app", type=Path, required=True)
    parser.add_argument("--source-runs", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--recorded-at", type=datetime.fromisoformat, required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    result = prepare_copy(
        source_app=args.source_app,
        source_runs=args.source_runs,
        output=args.output,
        recorded_at=args.recorded_at,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
