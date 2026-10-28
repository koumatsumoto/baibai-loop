"""EDINET list receipt union and first-retained archive handoff, without fact copying."""

from __future__ import annotations

import sqlite3
from contextlib import closing
from datetime import UTC, date, datetime
from pathlib import Path

from baibai_engine.machine_analysis.observations.disclosures.edinet_list_rebuild import rebuild_edinet_document_day
from baibai_engine.market.edinet_metrics.store import read_unrechecked_edinet_document_dates
from baibai_engine.market.receipt_handoff import copy_originals
from baibai_engine.storage.observations.schema import open_connection, validate_current_schema


def archive_keys(database: Path) -> set[tuple[str, int]]:
    with closing(sqlite3.connect(database.resolve().as_uri() + "?mode=ro", uri=True)) as conn:
        validate_current_schema(conn)
        return set(conn.execute("SELECT doc_id, source_type FROM edinet_source_archives"))


def export_handoff(
    source: Path, output: Path, *, since: datetime, archives: set[tuple[str, int]]
) -> dict[str, object]:
    if output.exists():
        raise FileExistsError(output)
    if since.tzinfo is None:
        raise ValueError("EDINET handoff cutoff requires a timezone")
    with (
        closing(sqlite3.connect(source.resolve().as_uri() + "?mode=ro", uri=True)) as raw,
        closing(open_connection(output)) as target,
        target,
    ):
        validate_current_schema(raw)
        raw.execute("BEGIN")
        lists = tuple(
            row[0]
            for row in raw.execute(
                "SELECT receipt_id FROM edinet_list_responses WHERE received_at_utc >= ? "
                "ORDER BY received_at_utc, receipt_id",
                (since.astimezone(UTC).isoformat(timespec="microseconds"),),
            )
        )
        copy_originals(
            raw, target, "edinet_list_responses", [(key,) for key in lists], selection="receipt_id"
        )
        copy_originals(raw, target, "edinet_source_archives", sorted(archives), selection="doc_id")
    return {"list_receipts": lists, "archives": sorted(archives)}


def import_handoff(source: Path, target: Path) -> dict[str, object]:
    with (
        closing(sqlite3.connect(source.resolve().as_uri() + "?mode=ro", uri=True)) as raw,
        closing(open_connection(target)) as conn,
    ):
        validate_current_schema(raw)
        lists = [(row[0],) for row in raw.execute("SELECT receipt_id FROM edinet_list_responses")]
        days = {
            date.fromisoformat(row[0])
            for row in raw.execute("SELECT DISTINCT doc_date FROM edinet_list_responses")
        }
        conflicts = []
        with conn:
            added = copy_originals(
                raw, conn, "edinet_list_responses", lists, selection="receipt_id"
            )
            # The existing archive owner keeps the first retained bytes. A collision
            # never overwrites fresh canonical bytes or silently extracts old input.
            for doc_id, source_type, stored_on, content in raw.execute(
                "SELECT * FROM edinet_source_archives"
            ):
                prior = conn.execute(
                    "SELECT zip_content FROM edinet_source_archives "
                    "WHERE doc_id = ? AND source_type = ?",
                    (doc_id, source_type),
                ).fetchone()
                if prior is not None:
                    if bytes(prior[0]) != bytes(content):
                        conflicts.append([doc_id, source_type])
                    continue
                conn.execute(
                    "INSERT INTO edinet_source_archives VALUES (?, ?, ?, ?)",
                    (doc_id, source_type, stored_on, content),
                )
                added += 1
        if days:
            days.update(read_unrechecked_edinet_document_dates(target, before=max(days)))
    failed = []
    for day in sorted(days):
        if rebuild_edinet_document_day(target, day).error is not None:
            failed.append(day.isoformat())
    return {
        "raw_rows_added": added,
        "replayed_days": [day.isoformat() for day in sorted(days)],
        "failed_days": failed,
        "archive_conflicts": conflicts,
    }
