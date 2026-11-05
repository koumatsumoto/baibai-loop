"""Rebuild disposable EDINET list rows from one fixed independent receipt head."""

from __future__ import annotations

from collections import defaultdict
from contextlib import closing
from dataclasses import replace
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from baibai_engine.data_collection.providers.edinet.list_native import (
    EdinetListL1,
    FixedEdinetLists,
)
from baibai_engine.data_collection.providers.edinet.list_projection import (
    document_list_rechecked_asof,
    project_document_list,
)
from baibai_engine.data_collection.providers.edinet.receipts import EdinetListReceipt
from baibai_engine.machine_analysis.observations.disclosures.edinet_list_rebuild import (
    store_edinet_document_projection,
)
from baibai_engine.storage.json import write_json_atomic
from baibai_engine.storage.sqlite.source_cache.schema import open_connection

_JST = ZoneInfo("Asia/Tokyo")


def materialize_lists(fixed: FixedEdinetLists, database: Path) -> dict[str, object]:
    """No live head or former L2 success participates in current source interpretation."""
    originals = tuple(fixed.iter_receipts())
    rows = {
        item.receipt_id: (
            item.receipt_id,
            item.doc_date.isoformat(),
            item.received_at.isoformat(timespec="microseconds"),
            item.body_gzip,
        )
        for item in originals
    }
    receipts: dict[date, list[EdinetListReceipt]] = defaultdict(list)
    for item in originals:
        receipts[item.doc_date].append(item.decoded())
    with closing(open_connection(database)) as conn, conn:
        conn.execute("BEGIN IMMEDIATE")
        # A forgotten old writer must not lose originals when a fixed head is read.
        for row in conn.execute("SELECT * FROM edinet_list_responses"):
            if rows.get(str(row[0])) != tuple(row):
                raise ValueError(
                    "market contains EDINET list originals outside the fixed native head"
                )
        legacy_days = {
            str(row[0])
            for row in conn.execute(
                "SELECT doc_date FROM edinet_document_lists "
                "UNION SELECT doc_date FROM edinet_documents"
            )
        }
        if legacy_days - {day.isoformat() for day in receipts}:
            raise ValueError(
                "EDINET legacy list dates lack original receipts; finish finite cutover"
            )
        conn.executemany(
            "INSERT OR IGNORE INTO edinet_list_responses VALUES (?, ?, ?, ?)", rows.values()
        )
        failed = []
        for day in sorted(receipts):
            projection = project_document_list(receipts[day])
            if projection.error is not None:
                failed.append(day.isoformat())
                if projection.selected_receipt is not None:
                    # Reproduce the same eligible prior on a cold or warm store.
                    # The malformed receipt remains raw and remains a reported failure.
                    projection = replace(
                        projection,
                        latest_received_at=projection.selected_receipt.received_at,
                        error=None,
                    )
            selected = projection.selected_receipt
            proof = (
                receipts.get(selected.received_at.astimezone(_JST).date(), [])
                if selected is not None
                else []
            )
            store_edinet_document_projection(
                database,
                projection,
                rechecked_asof=document_list_rechecked_asof(projection, proof),
                connection=conn,
            )
    return {
        "exit_code": int(bool(failed)),
        "status": "failed" if failed else "completed",
        "l1_identity": fixed.identity,
        "receipt_count": len(originals),
        "days": len(receipts),
        "failed_days": failed,
    }


def clear_verified_receipt_cache(fixed: FixedEdinetLists, database: Path) -> int:
    """Only discard this disposable copy after exact source-original readback."""
    with closing(open_connection(database)) as conn, conn:
        conn.execute("BEGIN IMMEDIATE")
        rows = {
            str(row[0]): tuple(row) for row in conn.execute("SELECT * FROM edinet_list_responses")
        }
        if not rows:
            return 0
        days = {date.fromisoformat(str(row[1])) for row in rows.values()}
        verified = set()
        for item in fixed.iter_receipts(days):
            if item.receipt_id not in rows:
                continue
            original = (
                item.receipt_id,
                item.doc_date.isoformat(),
                item.received_at.isoformat(timespec="microseconds"),
                item.body_gzip,
            )
            if rows[item.receipt_id] != original:
                raise ValueError("EDINET receipt cache differs from independent original")
            verified.add(item.receipt_id)
        if verified != rows.keys():
            raise ValueError("EDINET receipt cache contains unpublished originals")
        conn.execute("DELETE FROM edinet_list_responses")
        return len(rows)


def export_pending_handoff(owner: EdinetListL1, *, asof: date, started_at: datetime) -> bool:
    """Preserve only uncertain raw publication work in the existing stage protocol."""
    if not (owner.directory / "pending.json").exists():
        return False
    receipts = owner.pending_receipts()
    output = owner.directory / "receipts.sqlite"
    if output.exists():
        raise FileExistsError(
            "native failure handoff already exists; preserve it for explicit recovery"
        )
    with closing(open_connection(output)) as conn, conn:
        conn.executemany(
            "INSERT INTO edinet_list_responses VALUES (?, ?, ?, ?)",
            [
                (
                    item.receipt_id,
                    item.doc_date.isoformat(),
                    item.received_at.isoformat(timespec="microseconds"),
                    item.body_gzip,
                )
                for item in receipts
            ],
        )
    write_json_atomic(
        owner.directory / "result.json",
        {
            "source": "edinet-list",
            "asof": asof.isoformat(),
            "started_at": started_at.isoformat(),
            "list_receipts": [item.receipt_id for item in receipts],
            "archives": [],
            "native_l1_pending": True,
            "acquisition": {"status": "failed"},
        },
    )
    return True
