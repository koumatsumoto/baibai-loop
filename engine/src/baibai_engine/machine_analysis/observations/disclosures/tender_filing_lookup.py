"""提案の原典調査用。保存済み日別一覧の受領範囲と資料の役割を渡す。"""

from __future__ import annotations

import sqlite3
from datetime import UTC, date, datetime, timedelta
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict

from baibai_engine.data_collection.providers.edinet.list_projection import decode_list_response
from baibai_engine.data_collection.providers.edinet.models import EDINETProviderError
from baibai_engine.machine_analysis.observations.disclosures.tender_filing_references import (
    TenderFilingReference,
    resolve_tender_filing_references,
)
from baibai_engine.primitives.time import JST


class SelectedListReceipt(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    receipt_id: str
    received_at: datetime


class TenderFilingLookup(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    scope: Literal["selected_list_references_not_offer_outcome_or_shareholder_rights"] = (
        "selected_list_references_not_offer_outcome_or_shareholder_rights"
    )
    status: Literal[
        "observed_lists", "no_eligible_list", "unreadable_list", "as_of_history_unavailable"
    ]
    checked_at: datetime
    window_start: date
    window_end: date
    tickers: tuple[str, ...]
    selected_list_days: int = 0
    latest_list_received_at: datetime | None = None
    references_by_ticker: dict[str, tuple[TenderFilingReference, ...]] | None = None
    origin_list_receipts: dict[date, SelectedListReceipt] | None = None


def read_tender_filing_references(
    connection: sqlite3.Connection | None, *, tickers: tuple[str, ...], known_at: datetime
) -> TenderFilingLookup | None:
    if not tickers:
        return None
    if known_at.utcoffset() is None:
        raise ValueError("EDINET filing reference cutoff requires a timezone")
    through = known_at.astimezone(JST).date()
    # Investigation window, not a regulatory deadline or a complete issuer history.
    start = through - timedelta(days=89)
    missing = TenderFilingLookup(
        status="no_eligible_list",
        checked_at=known_at,
        window_start=start,
        window_end=through,
        tickers=tickers,
    )
    if connection is None:
        return missing
    cutoff = known_at.astimezone(UTC).isoformat(timespec="microseconds")
    bounds = (start.isoformat(), through.isoformat())
    # One current projection per day retains neither superseded selections nor
    # intervening failures. Dropping that day could resurrect a withdrawn filing.
    later = connection.execute(
        "SELECT 1 FROM edinet_document_lists WHERE doc_date BETWEEN ? AND ? "
        "AND latest_received_at_utc > ? LIMIT 1",
        (*bounds, cutoff),
    ).fetchone()
    if later is not None:
        return missing.model_copy(update={"status": "as_of_history_unavailable"})
    rows = connection.execute(
        "SELECT doc_date, selected_receipt_id, selected_received_at_utc, latest_received_at_utc, "
        "result_count, response_json FROM edinet_document_lists WHERE doc_date BETWEEN ? AND ? "
        "ORDER BY doc_date",
        bounds,
    )
    documents: list[dict[str, Any]] = []
    receipts: dict[date, SelectedListReceipt] = {}
    try:
        for day_str, receipt_id, selected_str, latest_str, count, source in rows:
            day = date.fromisoformat(day_str)
            if (
                not isinstance(receipt_id, str)
                or not selected_str
                or not latest_str
                or not isinstance(source, str)
            ):
                raise ValueError("missing selected list evidence")
            UUID(receipt_id)
            selected, latest = (
                datetime.fromisoformat(selected_str),
                datetime.fromisoformat(latest_str),
            )
            if selected.utcoffset() != timedelta(0) or latest.utcoffset() != timedelta(0):
                raise ValueError("invalid list clock")
            if selected != latest or latest > known_at:
                # A later failed refresh may hide an edit or withdrawal.
                raise ValueError("unusable selected list clock")
            _, records, _ = decode_list_response(source, doc_date=day)
            if len(records) != count:
                raise ValueError("stored EDINET result count differs")
            receipts[day] = SelectedListReceipt(receipt_id=receipt_id, received_at=selected)
            for record in records:
                record["doc_date"] = day_str
            documents.extend(records)
        if not receipts:
            return missing
        references = resolve_tender_filing_references(documents, tickers=tickers, known_at=known_at)
    except (TypeError, ValueError, EDINETProviderError):
        return missing.model_copy(update={"status": "unreadable_list"})
    origin_days = {ref.source_date for values in references.values() for ref in values}
    return TenderFilingLookup(
        status="observed_lists",
        checked_at=known_at,
        window_start=start,
        window_end=through,
        tickers=tickers,
        selected_list_days=len(receipts),
        latest_list_received_at=max(receipt.received_at for receipt in receipts.values()),
        references_by_ticker=references,
        origin_list_receipts={day: receipts[day] for day in sorted(origin_days)},
    )
