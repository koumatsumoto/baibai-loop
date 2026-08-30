"""Build a current v18 application store from one verified v17 source.

This one-shot operator tool removes duplicate judgment fields and retired single-step
operation sessions without adding a runtime compatibility path. Canonical ledger,
thesis, review, and retained operation rows are copied byte-for-byte.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
from collections.abc import Iterable
from decimal import Decimal
from pathlib import Path

from baibai_engine.appdb.json import canonical_json
from baibai_engine.appdb.schema import APPLICATION_SCHEMA_VERSION, SCHEMA_SQL
from baibai_engine.research.assessment import (
    BargainAssessment,
    assessment_draft_sha256,
    derive_case_machine_values,
)
from baibai_engine.research.thesis import ThesisDocument
from baibai_engine.screening.shortlist import Shortlist

SOURCE_VERSION = 17
_RETIRED_KINDS = frozenset({"pending-result", "monthly-contribution", "annual-outcome"})
_MACHINE_TOLERANCE = Decimal("0.005")

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
    "thesis": (
        "thesis_id",
        "ticker",
        "as_of",
        "recommendation",
        "published_at",
        "supersedes_id",
        "payload",
        "core_sha256",
    ),
    "thesis_review": ("review_id", "thesis_id", "reviewed_at", "payload"),
    "holding_review": (
        "holding_review_id",
        "ticker",
        "as_of",
        "thesis_id",
        "candidate_thesis_id",
        "payload",
    ),
    "ledger_event": (
        "append_seq",
        "event_id",
        "occurred_at",
        "same_instant_order",
        "event_type",
        "ticker",
        "decision_reference",
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


def _copy_rows(destination: sqlite3.Connection, table: str, columns: tuple[str, ...]) -> None:
    names = ", ".join(columns)
    destination.execute(
        f"INSERT INTO {table} ({names}) SELECT {names} FROM source.{table}"  # nosec B608
    )


def _shortlist_rows(connection: sqlite3.Connection) -> Iterable[tuple[object, ...]]:
    for row in connection.execute(
        "SELECT shortlist_id, selection_id, run_revision_id, as_of, published_at, payload "
        "FROM source.shortlist ORDER BY shortlist_id"
    ):
        payload = _object(row[5], label=f"shortlist {row[0]}")
        if payload.get("schema_version") != 6:
            raise CutoverError(f"shortlist {row[0]} is not schema_version 6")
        entries = payload.get("entries")
        if not isinstance(entries, list):
            raise CutoverError(f"shortlist {row[0]} entries must be a list")
        projected_entries: list[dict[str, object]] = []
        for entry in entries:
            if not isinstance(entry, dict):
                raise CutoverError(f"shortlist {row[0]} entry must be an object")
            projected = dict(entry)
            projected.pop("reject_class", None)
            projected_entries.append(projected)
        projected_payload = {**payload, "schema_version": 7, "entries": projected_entries}
        validated = Shortlist.model_validate(projected_payload)
        yield (*row[:5], canonical_json(validated.payload()))


def _assessment_rows(
    connection: sqlite3.Connection,
) -> tuple[list[tuple[object, ...]], int]:
    transformed: list[tuple[object, ...]] = []
    compared_cases = 0
    for row in connection.execute(
        "SELECT assessment_id, as_of, published_at, result, shortlist_id, payload "
        "FROM source.bargain_assessment ORDER BY assessment_id"
    ):
        payload = _object(row[5], label=f"assessment {row[0]}")
        if payload.get("schema_version") != 4:
            raise CutoverError(f"assessment {row[0]} is not schema_version 4")
        cases = payload.get("cases")
        if not isinstance(cases, list):
            raise CutoverError(f"assessment {row[0]} cases must be a list")
        projected_cases: list[dict[str, object]] = []
        for case in cases:
            if not isinstance(case, dict):
                raise CutoverError(f"assessment {row[0]} case must be an object")
            _verify_machine_copy(connection, str(row[0]), case)
            compared_cases += 1
            projected = dict(case)
            projected.pop("reject_class", None)
            projected.pop("machine", None)
            projected_cases.append(projected)
        projected_payload = {**payload, "schema_version": 5, "cases": projected_cases}
        review = projected_payload.get("review")
        if not isinstance(review, dict):
            raise CutoverError(f"assessment {row[0]} review must be an object")
        projected_review = dict(review)
        projected_payload["review"] = projected_review
        draft = BargainAssessment.model_validate(projected_payload)
        projected_review["draft_sha256"] = assessment_draft_sha256(draft)
        validated = BargainAssessment.model_validate(projected_payload)
        transformed.append((*row[:5], canonical_json(validated.payload())))
    return transformed, compared_cases


def _verify_machine_copy(
    connection: sqlite3.Connection,
    assessment_id: str,
    case: dict[str, object],
) -> None:
    thesis_id = case.get("thesis_id")
    row = connection.execute(
        "SELECT payload, core_sha256 FROM source.thesis WHERE thesis_id = ?", (thesis_id,)
    ).fetchone()
    if row is None:
        raise CutoverError(f"assessment {assessment_id} thesis is unavailable: {thesis_id}")
    if str(row[1]) != case.get("thesis_core_sha256"):
        raise CutoverError(f"assessment {assessment_id} thesis binding moved: {thesis_id}")
    machine = case.get("machine")
    if not isinstance(machine, dict):
        raise CutoverError(f"assessment {assessment_id} has no machine copy: {thesis_id}")
    document = ThesisDocument.model_validate(json.loads(str(row[0])))
    derived = derive_case_machine_values(document)
    drifted = [
        field
        for field, expected in derived.items()
        if field not in machine or not _values_agree(machine[field], expected)
    ]
    extra = sorted(set(machine) - set(derived))
    if drifted or extra:
        fields = sorted((*drifted, *extra))
        raise CutoverError(
            f"assessment {assessment_id} machine copy differs from thesis {thesis_id}: "
            + ", ".join(fields)
        )


def _values_agree(left: object, right: object) -> bool:
    if isinstance(left, str) or isinstance(right, str):
        return left == right
    if isinstance(left, list | tuple) and isinstance(right, list | tuple):
        return list(left) == list(right)
    if isinstance(left, list | tuple) or isinstance(right, list | tuple):
        return False
    if left is None or right is None:
        return left is None and right is None
    try:
        return abs(Decimal(str(left)) - Decimal(str(right))) <= _MACHINE_TOLERANCE
    except (ArithmeticError, ValueError):
        return False


def _operation_rows(
    connection: sqlite3.Connection,
) -> tuple[list[tuple[object, ...]], list[dict[str, object]]]:
    retained: list[tuple[object, ...]] = []
    dropped: list[dict[str, object]] = []
    rows = connection.execute(
        "SELECT operation_id, session_kind, status, as_of, ticker, started_at, completed_at, "
        "payload FROM source.operation_session ORDER BY operation_id"
    )
    for row in rows:
        kind = str(row[1])
        if kind not in _RETIRED_KINDS:
            retained.append(tuple(row))
            continue
        if row[2] != "completed":
            raise CutoverError(f"retired operation is still active: {row[0]} ({kind})")
        references = _resolve_retired_operation(connection, kind, row[7])
        dropped.append(
            {"operation_id": str(row[0]), "session_kind": kind, "canonical_refs": references}
        )
    return retained, dropped


def _resolve_retired_operation(
    connection: sqlite3.Connection,
    kind: str,
    raw_payload: object,
) -> list[str]:
    payload = _object(raw_payload, label=f"{kind} operation")
    raw_refs = payload.get("canonical_refs")
    if not isinstance(raw_refs, list) or not all(isinstance(item, str) for item in raw_refs):
        raise CutoverError(f"{kind} operation canonical_refs must be strings")
    refs = [str(item) for item in raw_refs]
    if kind in {"pending-result", "monthly-contribution"}:
        event_ids = [
            item.removeprefix("ledger_event: ")
            for item in refs
            if item.startswith("ledger_event: ")
        ]
        if not event_ids:
            raise CutoverError(f"{kind} operation has no canonical ledger event")
        placeholders = ", ".join("?" for _ in event_ids)
        rows = connection.execute(
            f"SELECT event_id, event_type FROM source.ledger_event "  # nosec B608
            f"WHERE event_id IN ({placeholders})",
            event_ids,
        ).fetchall()
        if len(rows) != len(set(event_ids)):
            raise CutoverError(f"{kind} operation references a missing ledger event")
        allowed = (
            {"reservation", "execution", "release"}
            if kind == "pending-result"
            else {"contribution"}
        )
        if any(str(row[1]) not in allowed for row in rows):
            raise CutoverError(f"{kind} operation references the wrong ledger event type")
        return sorted(f"ledger_event: {row[0]}" for row in rows)
    outcome_ids = [
        item.removeprefix("portfolio_outcome: ")
        for item in refs
        if item.startswith("portfolio_outcome: ")
    ]
    if not outcome_ids:
        raise CutoverError("annual-outcome operation has no canonical portfolio outcome")
    placeholders = ", ".join("?" for _ in outcome_ids)
    rows = connection.execute(
        f"SELECT outcome_id FROM source.portfolio_outcome "  # nosec B608
        f"WHERE outcome_id IN ({placeholders})",
        outcome_ids,
    ).fetchall()
    if len(rows) != len(set(outcome_ids)):
        raise CutoverError("annual-outcome operation references a missing portfolio outcome")
    return sorted(f"portfolio_outcome: {row[0]}" for row in rows)


def _object(raw: object, *, label: str) -> dict[str, object]:
    payload = json.loads(str(raw))
    if not isinstance(payload, dict):
        raise CutoverError(f"{label} payload must be an object")
    return payload


def cutover(source: Path, output: Path) -> dict[str, object]:
    """Create ``output`` from one verified v17 backup and report proved changes."""
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
        if connection.execute("PRAGMA source.integrity_check").fetchone() != ("ok",):
            raise CutoverError("source integrity_check failed")
        if connection.execute("PRAGMA source.foreign_key_check").fetchall():
            raise CutoverError("source foreign_key_check failed")
        connection.executescript(SCHEMA_SQL)
        connection.execute("BEGIN IMMEDIATE")
        connection.execute("PRAGMA defer_foreign_keys = ON")
        for table, columns in _UNCHANGED_TABLES.items():
            _copy_rows(connection, table, columns)
        shortlist_rows = list(_shortlist_rows(connection))
        connection.executemany("INSERT INTO shortlist VALUES (?, ?, ?, ?, ?, ?)", shortlist_rows)
        assessment_rows, compared_cases = _assessment_rows(connection)
        connection.executemany(
            "INSERT INTO bargain_assessment VALUES (?, ?, ?, ?, ?, ?)", assessment_rows
        )
        operation_rows, dropped_operations = _operation_rows(connection)
        connection.executemany(
            "INSERT INTO operation_session VALUES (?, ?, ?, ?, ?, ?, ?, ?)", operation_rows
        )
        violations = connection.execute("PRAGMA foreign_key_check").fetchall()
        if violations:
            raise CutoverError(f"foreign_key_check failed: {violations!r}")
        connection.execute(f"PRAGMA user_version = {APPLICATION_SCHEMA_VERSION}")
        connection.commit()
        if connection.execute("PRAGMA integrity_check").fetchone() != ("ok",):
            raise CutoverError("output integrity_check failed")
        counts = {
            table: int(connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0])  # nosec B608
            for table in (
                *_UNCHANGED_TABLES,
                "shortlist",
                "bargain_assessment",
                "operation_session",
            )
        }
        for table, count in counts.items():
            source_count = int(
                connection.execute(
                    f"SELECT count(*) FROM source.{table}"  # nosec B608
                ).fetchone()[0]
            )
            expected = (
                source_count - len(dropped_operations)
                if table == "operation_session"
                else source_count
            )
            if count != expected:
                raise CutoverError(f"row count changed for {table}: {source_count} -> {count}")
        return {
            "schema_version": APPLICATION_SCHEMA_VERSION,
            "rows": counts,
            "assessment_machine_cases_compared": compared_cases,
            "dropped_operations": dropped_operations,
        }
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
    print(json.dumps(cutover(args.source, args.output), ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
