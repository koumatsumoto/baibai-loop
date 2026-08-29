"""Build a current v17 application store from one verified v16 source.

This one-shot operator tool produces the current decision store without adding a
runtime compatibility path. It preserves canonical rows and ledger payloads while
removing Proposal storage and normalizing shortlist/assessment payloads.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
from collections.abc import Iterable
from pathlib import Path

from baibai_engine.appdb.schema import APPLICATION_SCHEMA_VERSION, SCHEMA_SQL
from baibai_engine.foundation.ranked_set import RESEARCH_GATE_CONTRACT_ID
from baibai_engine.research.assessment import BargainAssessment
from baibai_engine.research.thesis import ThesisDocument
from baibai_engine.screening.shortlist import Shortlist

SOURCE_VERSION = 16

_UNCHANGED_TABLES: dict[str, tuple[str, ...]] = {
    "task": (
        "task_id",
        "status",
        "kind",
        "ticker",
        "due_date",
        "event_date",
        "created_at",
        "closed_at",
        "payload",
    ),
    "macro_context": (
        "context_id",
        "schema_version",
        "as_of",
        "published_at",
        "supersedes_id",
        "payload",
    ),
    "macro_context_head": ("singleton", "context_id"),
    "thesis_review": ("review_id", "thesis_id", "reviewed_at", "payload"),
    "holding_review": (
        "holding_review_id",
        "ticker",
        "as_of",
        "thesis_id",
        "candidate_thesis_id",
        "payload",
    ),
    "operation_session": (
        "operation_id",
        "session_kind",
        "status",
        "as_of",
        "ticker",
        "started_at",
        "completed_at",
        "payload",
    ),
    "ledger_market_price": (
        "ticker",
        "observed_at",
        "price_yen",
        "source_kind",
        "price_basis",
        "source_ref",
        "payload",
    ),
    "ledger_meta": (
        "singleton",
        "schema_version",
        "portfolio_scope",
        "as_of",
        "estimated_exit_tax_rate_bps",
        "estimated_exit_tax_basis",
        "payload",
    ),
    "portfolio_outcome": (
        "outcome_id",
        "horizon",
        "period_start_date",
        "period_end_date",
        "status",
        "payload",
    ),
}


class CutoverError(RuntimeError):
    """The source cannot be converted without guessing or losing canonical data."""


def _canonical_json(payload: dict[str, object]) -> str:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _shortlist_payload(raw: str) -> str | None:
    payload = json.loads(raw)
    if not isinstance(payload, dict) or payload.get("schema_version") not in {2, 3, 4, 5, 6}:
        raise CutoverError("shortlist payload is not a supported canonical revision")
    if payload.get("schema_version") in {2, 3}:
        return None
    projected = dict(payload)
    projected["schema_version"] = 6
    projected.setdefault("review_basis_shortlist_id", None)
    projected["research_gate_contract_id"] = RESEARCH_GATE_CONTRACT_ID
    projected.pop("profile", None)
    for key in (
        "attention_policy_id",
        "attention_policy_hash",
        "attention_policy_parameters",
        "attention_provenance_status",
    ):
        projected.pop(key, None)
    entries = projected.get("entries")
    if not isinstance(entries, list):
        raise CutoverError("shortlist entries must be a list")
    for entry in entries:
        if not isinstance(entry, dict):
            raise CutoverError("shortlist entry must be an object")
        entry.pop("lane_provenance_status", None)
        snapshot = entry.get("machine_snapshot")
        if not isinstance(snapshot, dict):
            continue
        if "primary_evidence_pattern_id" not in snapshot:
            snapshot["primary_evidence_pattern_id"] = snapshot.get("screening_playbook")
        for key in (
            "opportunity_lane_id",
            "selection_policy_id",
            "selection_policy_hash",
            "lane_rank",
            "lane_native_value",
            "lane_native_unit",
            "baseline_er_rank",
            "screening_playbook",
            "policy_diagnostic_ids",
            "lane_provenance_status",
        ):
            snapshot.pop(key, None)
    validated = Shortlist.model_validate(projected)
    return _canonical_json(validated.payload())


def _assessment_payload(raw: str) -> str | None:
    payload = json.loads(raw)
    if not isinstance(payload, dict) or payload.get("schema_version") not in {1, 2, 3, 4}:
        raise CutoverError("assessment payload is not a supported canonical revision")
    if payload.get("schema_version") in {1, 2}:
        return None
    projected = dict(payload)
    projected["schema_version"] = 4
    projected["result"] = (
        "buy" if projected.get("result") == "proposal" else projected.get("result")
    )
    if "cases" not in projected:
        projected["cases"] = projected.pop("lanes", [])
    else:
        projected.pop("lanes", None)
    projected.pop("purchase", None)
    projected.pop("entry_timing", None)
    validated = BargainAssessment.model_validate(projected)
    return _canonical_json(validated.payload())


def _thesis_payload(raw: str) -> str:
    payload = json.loads(raw)
    if not isinstance(payload, dict):
        raise CutoverError("thesis payload must be an object")
    projected = dict(payload)
    estimates = projected.get("estimates")
    if not isinstance(estimates, dict):
        raise CutoverError("thesis estimates must be an object")
    estimates = dict(estimates)
    estimates.pop("deep_discount_bps", None)
    projected["estimates"] = estimates
    judgment = projected.get("judgment")
    if not isinstance(judgment, dict):
        raise CutoverError("thesis judgment must be an object")
    judgment = dict(judgment)
    judgment.pop("position_intent", None)
    projected["judgment"] = judgment
    ThesisDocument.model_validate(projected)
    return _canonical_json(projected)


def _copy_rows(destination: sqlite3.Connection, table: str, columns: tuple[str, ...]) -> None:
    names = ", ".join(columns)
    # Names come from _UNCHANGED_TABLES, not operator input.
    destination.execute(
        f"INSERT INTO {table} ({names}) SELECT {names} FROM source.{table}"  # nosec B608
    )


def _transformed_rows(
    connection: sqlite3.Connection, table: str, transform: object
) -> Iterable[tuple[object, ...]]:
    # Table is a cutover-owned literal, not operator input.
    rows = connection.execute(
        f"SELECT * FROM source.{table}"  # nosec B608
    )
    names = tuple(item[0] for item in rows.description or ())
    payload_index = names.index("payload")
    result_index = names.index("result") if table == "bargain_assessment" else None
    for row in rows:
        values = list(row)
        transformed = transform(str(values[payload_index]))  # type: ignore[operator]
        if transformed is None:
            continue
        values[payload_index] = transformed
        if result_index is not None and values[result_index] == "proposal":
            values[result_index] = "buy"
        yield tuple(values)


def cutover(source: Path, output: Path) -> dict[str, int]:
    """Create ``output`` and return verified current-table row counts."""
    if output.exists():
        raise CutoverError(f"output already exists: {output}")
    if not source.is_file():
        raise CutoverError(f"source does not exist: {source}")
    output.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(output)
    try:
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("ATTACH DATABASE ? AS source", (str(source.resolve()),))
        version = int(connection.execute("PRAGMA source.user_version").fetchone()[0])
        if version != SOURCE_VERSION:
            raise CutoverError(f"source user_version is {version}; expected {SOURCE_VERSION}")
        integrity = connection.execute("PRAGMA source.integrity_check").fetchone()
        if integrity != ("ok",):
            raise CutoverError(f"source integrity_check failed: {integrity!r}")
        connection.executescript(SCHEMA_SQL)
        # Source tables contain cross-table and self-referential publication links.
        # Copy order must not decide whether a valid source survives the cutover; the
        # complete destination is checked before commit below.
        connection.execute("PRAGMA defer_foreign_keys = ON")
        connection.execute("BEGIN IMMEDIATE")
        for table, columns in _UNCHANGED_TABLES.items():
            _copy_rows(connection, table, columns)
        thesis_columns = (
            "thesis_id",
            "ticker",
            "as_of",
            "recommendation",
            "published_at",
            "supersedes_id",
            "payload",
            "core_sha256",
        )
        thesis_placeholders = ", ".join("?" for _ in thesis_columns)
        connection.executemany(
            f"INSERT INTO thesis ({', '.join(thesis_columns)}) "  # nosec B608
            f"VALUES ({thesis_placeholders})",
            _transformed_rows(connection, "thesis", _thesis_payload),
        )
        shortlist_columns = (
            "shortlist_id",
            "selection_id",
            "run_revision_id",
            "as_of",
            "published_at",
            "payload",
        )
        shortlist_placeholders = ", ".join("?" for _ in shortlist_columns)
        connection.executemany(
            f"INSERT INTO shortlist ({', '.join(shortlist_columns)}) "  # nosec B608
            f"VALUES ({shortlist_placeholders})",
            _transformed_rows(connection, "shortlist", _shortlist_payload),
        )
        assessment_columns = (
            "assessment_id",
            "as_of",
            "published_at",
            "result",
            "shortlist_id",
            "payload",
        )
        assessment_placeholders = ", ".join("?" for _ in assessment_columns)
        connection.executemany(
            f"INSERT INTO bargain_assessment ({', '.join(assessment_columns)}) "  # nosec B608
            f"VALUES ({assessment_placeholders})",
            _transformed_rows(connection, "bargain_assessment", _assessment_payload),
        )
        connection.execute(
            "INSERT INTO ledger_event (append_seq, event_id, occurred_at, same_instant_order, "
            "event_type, ticker, decision_reference, payload) "
            "SELECT append_seq, event_id, occurred_at, same_instant_order, event_type, ticker, "
            "proposal_id, payload FROM source.ledger_event"
        )
        violations = connection.execute("PRAGMA foreign_key_check").fetchall()
        if violations:
            raise CutoverError(f"foreign_key_check failed: {violations!r}")
        connection.execute(f"PRAGMA user_version = {APPLICATION_SCHEMA_VERSION}")
        connection.commit()
        if connection.execute("PRAGMA integrity_check").fetchone() != ("ok",):
            raise CutoverError("output integrity_check failed")
        counts = {
            table: int(
                connection.execute(
                    f"SELECT count(*) FROM {table}"  # nosec B608
                ).fetchone()[0]
            )
            for table in (
                *_UNCHANGED_TABLES,
                "thesis",
                "shortlist",
                "bargain_assessment",
                "ledger_event",
            )
        }
        for table, count in counts.items():
            if table == "shortlist":
                source_count = int(
                    connection.execute(
                        "SELECT count(*) FROM source.shortlist WHERE "
                        "json_extract(payload, '$.schema_version') IN (4, 5, 6)"
                    ).fetchone()[0]
                )
            elif table == "bargain_assessment":
                source_count = int(
                    connection.execute(
                        "SELECT count(*) FROM source.bargain_assessment WHERE "
                        "json_extract(payload, '$.schema_version') IN (3, 4)"
                    ).fetchone()[0]
                )
            else:
                source_count = int(
                    connection.execute(
                        f"SELECT count(*) FROM source.{table}"  # nosec B608
                    ).fetchone()[0]
                )
            if count != source_count:
                raise CutoverError(f"row count changed for {table}: {source_count} -> {count}")
        return counts
    except BaseException:
        connection.close()
        output.unlink(missing_ok=True)
        raise
    finally:
        if output.exists():
            connection.close()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    counts = cutover(args.source, args.output)
    print(
        json.dumps({"schema_version": APPLICATION_SCHEMA_VERSION, "rows": counts}, sort_keys=True)
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
