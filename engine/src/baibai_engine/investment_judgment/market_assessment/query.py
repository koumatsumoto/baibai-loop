"""保存済み本文の履歴取得と、明示した利用可能時刻での分析入力選択。"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import replace
from datetime import UTC, date, datetime
from pathlib import Path

from baibai_engine.investment_judgment.market_assessment.models import (
    ASSESSMENT_SCHEMA_VERSION,
    RECEIPTED_ENVELOPE_VERSION,
    StoredAssessment,
    require_aware,
)
from baibai_engine.storage.sqlite.application.paths import database_path
from baibai_engine.storage.sqlite.application.read import connect_read_only
from baibai_engine.storage.sqlite.read import is_unwritten_store


def assessment_history(path: Path | None) -> list[StoredAssessment]:
    resolved = database_path(path)
    if not resolved.is_file():
        return []
    connection = connect_read_only(resolved)
    try:
        try:
            rows = connection.execute(
                "SELECT context_id, schema_version, as_of, published_at, payload "
                "FROM market_context_assessment"
            ).fetchall()
        except sqlite3.OperationalError as error:
            if is_unwritten_store(error, resolved):
                return []
            raise
        records = [stored_assessment_from_row(row) for row in rows]
        return sorted(
            records,
            key=lambda r: (
                (r.recorded_at or r.published_at).astimezone(UTC),
                r.as_of,
                r.context_id,
            ),
            reverse=True,
        )
    finally:
        connection.close()


def assessment_by_id(path: Path | None, *, context_id: str) -> StoredAssessment:
    """History access is independent of analytical date/cutoff eligibility."""
    resolved = database_path(path)
    if not resolved.is_file():
        raise FileNotFoundError(f"application database not found: {resolved}")
    connection = connect_read_only(resolved)
    try:
        try:
            row = connection.execute(
                "SELECT context_id, schema_version, as_of, published_at, payload "
                "FROM market_context_assessment WHERE context_id=?",
                (context_id,),
            ).fetchone()
        except sqlite3.OperationalError as error:
            if is_unwritten_store(error, resolved):
                raise FileNotFoundError(f"unknown context_id: {context_id}") from error
            raise
        if row is None:
            raise FileNotFoundError(f"unknown context_id: {context_id}")
        return stored_assessment_from_row(row)
    finally:
        connection.close()


def stored_assessment_from_row(row: sqlite3.Row) -> StoredAssessment:
    payload = json.loads(str(row["payload"]))
    if not isinstance(payload, dict):
        raise ValueError("assessment payload must be an object")
    record = StoredAssessment.from_payload(payload)
    if (record.context_id, record.as_of.isoformat()) != (row["context_id"], row["as_of"]):
        raise ValueError("assessment publication identity differs")
    version = int(row["schema_version"])
    if version == RECEIPTED_ENVELOPE_VERSION:
        recorded = require_aware(datetime.fromisoformat(str(row["published_at"])))
        if record.schema_version != ASSESSMENT_SCHEMA_VERSION or recorded < record.published_at:
            raise ValueError("assessment receipt differs from its authored publication")
        return replace(record, recorded_at=recorded)
    if record.schema_version != version or record.published_at.isoformat() != row["published_at"]:
        raise ValueError("assessment publication identity differs")
    return record


def latest_assessment(
    path: Path | None, *, as_of: date, known_at: datetime
) -> StoredAssessment | None:
    """Select by publication metadata, then validate only the selected body.

    Unused historical/future bodies are not prerequisites for a current Research
    input. A broken selected body still fails; never fall back to an older judgment.
    """
    require_aware(known_at)
    resolved = database_path(path)
    if not resolved.is_file():
        return None
    connection = connect_read_only(resolved)
    try:
        connection.execute("BEGIN")
        try:
            return latest_assessment_from_connection(connection, as_of=as_of, known_at=known_at)
        except sqlite3.OperationalError as error:
            if is_unwritten_store(error, resolved):
                return None
            raise
    finally:
        connection.close()


def latest_assessment_from_connection(
    connection: sqlite3.Connection,
    *,
    as_of: date,
    known_at: datetime,
    require_receipt: bool = False,
) -> StoredAssessment | None:
    """呼出元のtransaction内で選ぶ。Operationの他の開始材料と断面を揃える。"""
    require_aware(known_at)
    rows = connection.execute(
        "SELECT context_id,as_of,published_at,schema_version FROM market_context_assessment "
        "WHERE schema_version IN (3,4,5,6,?) AND as_of<=?",
        (RECEIPTED_ENVELOPE_VERSION, as_of.isoformat()),
    )
    eligible: list[tuple[datetime, date, str]] = []
    unknown: set[str] = set()
    for row in rows:
        instant = require_aware(datetime.fromisoformat(str(row["published_at"])))
        if instant > known_at:
            continue
        identity = str(row["context_id"])
        if require_receipt and row["schema_version"] != RECEIPTED_ENVELOPE_VERSION:
            unknown.add(identity)
        else:
            eligible.append(
                (instant.astimezone(UTC), date.fromisoformat(str(row["as_of"])), identity)
            )
    selected_id = max(eligible)[2] if eligible else None
    if unknown:
        # A superseded ancestor indisputably existed before this trusted publication.
        # An independent or later old-writer row has no receipt; don't call it absent
        # or silently declare some other judgment was the most recent at the cutoff.
        ancestors = set()
        if selected_id is not None:
            ancestors = {
                row[0]
                for row in connection.execute(
                    "WITH RECURSIVE ancestry(id) AS (SELECT supersedes_id "
                    "FROM market_context_assessment WHERE context_id=? UNION "
                    "SELECT m.supersedes_id FROM market_context_assessment m JOIN ancestry a "
                    "ON m.context_id=a.id) SELECT id FROM ancestry WHERE id IS NOT NULL",
                    (selected_id,),
                )
            }
        if unknown - ancestors:
            raise ValueError(
                "market publication receipt unknown for historical judgment: "
                + ", ".join(sorted(unknown - ancestors))
            )
    if selected_id is None:
        return None
    selected = connection.execute(
        "SELECT context_id,schema_version,as_of,published_at,payload "
        "FROM market_context_assessment WHERE context_id=?",
        (selected_id,),
    ).fetchone()
    assert selected is not None  # The metadata and body share one read snapshot.
    return stored_assessment_from_row(selected)
