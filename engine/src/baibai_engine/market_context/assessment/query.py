"""保存済み本文の履歴取得と、明示した利用可能時刻での分析入力選択。"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

from baibai_engine.foundation.sqlite_read import is_unwritten_store
from baibai_engine.storage.application.paths import database_path
from baibai_engine.storage.application.read import connect_read_only

from .models import StoredAssessment, require_aware


def select_assessment(
    records: Iterable[StoredAssessment], *, as_of: date, known_at: datetime
) -> StoredAssessment | None:
    """Pure real-instant selector. The caller fixes known_at; no clock is read here."""
    require_aware(known_at)
    eligible = [
        r
        for r in records
        if r.schema_version in (3, 4, 5)
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
                "SELECT context_id, schema_version, as_of, published_at, payload FROM macro_context"
            ).fetchall()
        except sqlite3.OperationalError as error:
            if is_unwritten_store(error, resolved):
                return []
            raise
        records = []
        for row in rows:
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
            records.append(record)
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
        raise ValueError(f"application database not found: {resolved}")
    connection = connect_read_only(resolved)
    try:
        try:
            row = connection.execute(
                "SELECT payload FROM macro_context WHERE context_id=?", (context_id,)
            ).fetchone()
        except sqlite3.OperationalError as error:
            if is_unwritten_store(error, resolved):
                raise ValueError(f"unknown context_id: {context_id}") from error
            raise
        if row is None:
            raise ValueError(f"unknown context_id: {context_id}")
        payload = json.loads(str(row[0]))
        if not isinstance(payload, dict):
            raise ValueError("assessment payload must be an object")
        record = StoredAssessment.from_payload(payload)
        if record.context_id != context_id:
            raise ValueError("assessment publication identity differs")
        return record
    finally:
        connection.close()


def latest_assessment(
    path: Path | None, *, as_of: date, known_at: datetime
) -> StoredAssessment | None:
    return select_assessment(assessment_history(path), as_of=as_of, known_at=known_at)


def assessment_payload(path: Path | None, *, context_id: str) -> dict[str, Any]:
    return assessment_by_id(path, context_id=context_id).document


def latest_assessment_payload(
    path: Path | None, *, as_of: date, known_at: datetime
) -> dict[str, Any] | None:
    record = latest_assessment(path, as_of=as_of, known_at=known_at)
    return None if record is None else record.document


def list_assessment_payloads(path: Path | None) -> list[dict[str, Any]]:
    return [record.document for record in assessment_history(path)]
