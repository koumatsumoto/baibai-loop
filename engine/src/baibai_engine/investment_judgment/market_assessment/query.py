"""保存済み本文の履歴取得と、明示した利用可能時刻での分析入力選択。"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

from baibai_engine.storage.sqlite.read import is_unwritten_store
from baibai_engine.storage.sqlite.application.paths import database_path
from baibai_engine.storage.sqlite.application.read import connect_read_only

from baibai_engine.investment_judgment.market_assessment.models import StoredAssessment, require_aware


def select_assessment(
    records: Iterable[StoredAssessment], *, as_of: date, known_at: datetime
) -> StoredAssessment | None:
    """Pure real-instant selector. The caller fixes known_at; no clock is read here."""
    require_aware(known_at)
    eligible = [
        r
        for r in records
        if r.schema_version in (3, 4, 5, 6)
        and r.as_of <= as_of
        and r.published_at.astimezone(UTC) <= known_at.astimezone(UTC)
    ]
    return max(
        eligible,
        key=lambda r: (r.published_at.astimezone(UTC), r.as_of, r.context_id),
        default=None,
    )


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
        records = [_stored_assessment(row) for row in rows]
        return sorted(
            records,
            key=lambda r: (r.published_at.astimezone(UTC), r.as_of, r.context_id),
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
        return _stored_assessment(row)
    finally:
        connection.close()


def _stored_assessment(row: sqlite3.Row) -> StoredAssessment:
    payload = json.loads(str(row["payload"]))
    if not isinstance(payload, dict):
        raise ValueError("assessment payload must be an object")
    record = StoredAssessment.from_payload(payload)
    if (
        record.context_id,
        record.schema_version,
        record.as_of.isoformat(),
        record.published_at.isoformat(),
    ) != (
        str(row["context_id"]),
        int(row["schema_version"]),
        str(row["as_of"]),
        str(row["published_at"]),
    ):
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
            rows = connection.execute(
                "SELECT context_id,as_of,published_at FROM market_context_assessment "
                "WHERE schema_version IN (3,4,5,6) AND as_of<=?",
                (as_of.isoformat(),),
            ).fetchall()
        except sqlite3.OperationalError as error:
            if is_unwritten_store(error, resolved):
                return None
            raise
        eligible: list[tuple[datetime, date, str]] = []
        for row in rows:
            published = require_aware(datetime.fromisoformat(str(row["published_at"])))
            if published.astimezone(UTC) <= known_at.astimezone(UTC):
                eligible.append(
                    (
                        published.astimezone(UTC),
                        date.fromisoformat(str(row["as_of"])),
                        str(row["context_id"]),
                    )
                )
        if not eligible:
            return None
        selected_id = max(eligible)[2]
        selected = connection.execute(
            "SELECT context_id,schema_version,as_of,published_at,payload "
            "FROM market_context_assessment WHERE context_id=?",
            (selected_id,),
        ).fetchone()
        assert selected is not None  # The metadata and body share one read snapshot.
        return _stored_assessment(selected)
    finally:
        connection.close()


def assessment_payload(path: Path | None, *, context_id: str) -> dict[str, Any]:
    return assessment_by_id(path, context_id=context_id).document


def latest_assessment_payload(
    path: Path | None, *, as_of: date, known_at: datetime
) -> dict[str, Any] | None:
    record = latest_assessment(path, as_of=as_of, known_at=known_at)
    return None if record is None else record.document


def list_assessment_payloads(path: Path | None) -> list[dict[str, Any]]:
    return [record.document for record in assessment_history(path)]
