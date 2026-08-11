"""EDINET ingest: documents and extracted metrics."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable, Mapping
from datetime import UTC, date, datetime
from math import isfinite
from pathlib import Path
from typing import Any

from baibai_engine.market.sqlite.convert import (
    date_iso,
    first,
    normalize_ticker_or_none,
    to_float,
    to_str_or_none,
)
from baibai_engine.market.sqlite.coverage import (
    date_range_row_count,
    delete_overlapping_source_coverage,
    record_source_coverage,
)
from baibai_engine.market.sqlite.schema import (
    EDINET_DOCUMENT_DESCRIPTIVE_COLUMNS,
    open_connection,
)


def store_edinet_documents(
    db_path: Path,
    on_date: date,
    records: Iterable[Mapping[str, Any]],
    *,
    process_datetime: str | None = None,
    result_count: int | None = None,
    is_final: bool = False,
) -> int:
    conn = open_connection(db_path)
    try:
        records_list = list(records)
        rows = _edinet_document_rows(
            on_date.isoformat(),
            records_list,
            observed=_observed_descriptive_columns(conn, on_date.isoformat()),
        )
        expected_count = len(records_list) if result_count is None else result_count
        if expected_count != len(records_list):
            raise ValueError(
                "EDINET document resultset count mismatch: "
                f"metadata={expected_count} results={len(records_list)}"
            )
        conn.execute("DELETE FROM edinet_documents WHERE doc_date = ?", (on_date.isoformat(),))
        delete_overlapping_source_coverage(conn, "edinet_documents", on_date, on_date)
        if rows:
            conn.executemany(
                "INSERT INTO edinet_documents("
                "doc_date, sequence_number, doc_id, sec_code, doc_type_code, csv_flag, "
                "xbrl_flag, legal_status, disclosure_status, withdrawal_status, "
                "doc_info_edit_status, parent_doc_id, operation_datetime, submit_datetime, "
                "doc_description, period_start, period_end"
                ", edinet_code, issuer_edinet_code, subject_edinet_code"
                ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                rows,
            )
        persisted_count = date_range_row_count(
            conn, "edinet_documents", "doc_date", on_date, on_date
        )
        record_source_coverage(
            conn,
            source="edinet_documents",
            operation="documents",
            coverage_key=on_date.isoformat(),
            coverage_start=on_date.isoformat(),
            coverage_end=on_date.isoformat(),
            requested_start=on_date.isoformat(),
            requested_end=on_date.isoformat(),
            params={"date": on_date.isoformat(), "type": 2},
            record_count=persisted_count,
            raw_record_count=len(records_list),
            skipped_record_count=0,
            status="ok",
            error=None,
        )
        conn.execute(
            "INSERT OR REPLACE INTO edinet_document_lists("
            "doc_date, process_datetime, result_count, fetched_at_utc, is_final"
            ") VALUES (?, ?, ?, ?, ?)",
            (
                on_date.isoformat(),
                process_datetime,
                expected_count,
                datetime.now(UTC).isoformat(),
                int(is_final),
            ),
        )
        conn.commit()
        return persisted_count
    finally:
        conn.close()


def store_edinet_metrics(
    db_path: Path,
    asof_date: date,
    records: Iterable[Mapping[str, Any]],
    *,
    status: str = "ok",
    error: str | None = None,
) -> int:
    conn = open_connection(db_path)
    try:
        records_list = list(records)
        rows = _edinet_metric_rows(asof_date.isoformat(), records_list)
        skipped_count = len(records_list) - len(rows)
        stored_status = status
        stored_error = error
        if status == "ok" and skipped_count:
            stored_status = "partial"
            stored_error = f"{skipped_count} EDINET metric rows were skipped"
        conn.execute("DELETE FROM edinet_metrics WHERE asof_date = ?", (asof_date.isoformat(),))
        delete_overlapping_source_coverage(conn, "edinet_metrics", asof_date, asof_date)
        if rows:
            conn.executemany(
                "INSERT OR REPLACE INTO edinet_metrics("
                "asof_date, ticker, sales_ttm, ocf_ttm, debt, cash, ebitda_ttm, "
                "consolidation_basis, ttm_quality_ev_ebitda, ttm_quality_p_s, "
                "ttm_quality_pcfr, operating_profit_ttm, depreciation_and_amortization_ttm, "
                "capex_ttm, fcf_ttm, net_cash, equity, total_assets, ttm_quality_fcf, "
                "ttm_quality_net_cash, source_doc_id, document_type, source_submit_datetime, "
                "source_period_start, source_period_end, capex_source, failure_reasons, "
                "extractor_revision, source_document_revision, investment_securities"
                ") VALUES ("
                "?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, "
                "?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?"
                ")",
                rows,
            )
        persisted_count = date_range_row_count(
            conn, "edinet_metrics", "asof_date", asof_date, asof_date
        )
        record_source_coverage(
            conn,
            source="edinet_metrics",
            operation="metrics",
            coverage_key=asof_date.isoformat(),
            coverage_start=asof_date.isoformat(),
            coverage_end=asof_date.isoformat(),
            requested_start=asof_date.isoformat(),
            requested_end=asof_date.isoformat(),
            params={"asof_date": asof_date.isoformat()},
            record_count=persisted_count,
            raw_record_count=len(records_list),
            skipped_record_count=skipped_count,
            status=stored_status,
            error=stored_error,
        )
        conn.commit()
        return persisted_count
    finally:
        conn.close()


def _observed_descriptive_columns(
    conn: sqlite3.Connection, doc_date: str
) -> dict[str, dict[str, str | None]]:
    """What this store already saw a day's filings say, keyed by document id.

    The document id rather than the list position, so that a day whose entries EDINET
    returns in a different order cannot graft one filing's description onto another.
    """
    rows = conn.execute(
        "SELECT doc_id, sec_code, doc_type_code, parent_doc_id, submit_datetime, "
        "doc_description FROM edinet_documents WHERE doc_date = ?",
        (doc_date,),
    ).fetchall()
    return {
        str(doc_id): dict(zip(EDINET_DOCUMENT_DESCRIPTIVE_COLUMNS, values, strict=True))
        for doc_id, *values in rows
    }


def _edinet_document_rows(
    doc_date: str,
    records: Iterable[Mapping[str, Any]],
    *,
    observed: Mapping[str, Mapping[str, str | None]],
) -> list[tuple[Any, ...]]:
    rows: list[tuple[Any, ...]] = []
    sequence_numbers: set[int] = set()
    for record in records:
        doc_id = to_str_or_none(first(record, "docID", "doc_id"))
        if doc_id is None:
            raise ValueError("EDINET document row is missing docID")
        raw_sequence = first(record, "seqNumber", "sequence_number")
        if raw_sequence in (None, ""):
            raise ValueError("EDINET document row is missing seqNumber")
        if isinstance(raw_sequence, bool):
            raise ValueError(f"invalid EDINET seqNumber: {raw_sequence!r}")
        if isinstance(raw_sequence, int):
            sequence_number = raw_sequence
        elif isinstance(raw_sequence, str) and raw_sequence.isdecimal():
            sequence_number = int(raw_sequence)
        else:
            raise ValueError(f"invalid EDINET seqNumber: {raw_sequence!r}")
        if sequence_number <= 0:
            raise ValueError(f"invalid EDINET seqNumber: {raw_sequence!r}")
        if sequence_number in sequence_numbers:
            raise ValueError(f"duplicate EDINET seqNumber: {sequence_number}")
        sequence_numbers.add(sequence_number)
        # A list read after the inspection period ended reports these five as null. The
        # stored answer from a read while the filing was still served stays true, so the
        # new response only ever adds to them. The lifecycle columns below take the new
        # answer unconditionally: an expiry or a withdrawal is the point of re-reading.
        retained = observed.get(doc_id, {})
        described = {
            column: value if value is not None else retained.get(column)
            for column, value in (
                ("sec_code", to_str_or_none(first(record, "secCode", "sec_code"))),
                ("doc_type_code", to_str_or_none(first(record, "docTypeCode", "doc_type_code"))),
                ("parent_doc_id", to_str_or_none(first(record, "parentDocID", "parent_doc_id"))),
                (
                    "submit_datetime",
                    to_str_or_none(first(record, "submitDateTime", "submit_datetime")),
                ),
                (
                    "doc_description",
                    to_str_or_none(first(record, "docDescription", "doc_description")),
                ),
            )
        }
        rows.append(
            (
                doc_date,
                sequence_number,
                doc_id,
                described["sec_code"],
                described["doc_type_code"],
                to_str_or_none(first(record, "csvFlag", "csv_flag")),
                to_str_or_none(first(record, "xbrlFlag", "xbrl_flag")),
                to_str_or_none(first(record, "legalStatus", "legal_status")),
                to_str_or_none(first(record, "disclosureStatus", "disclosure_status")),
                to_str_or_none(first(record, "withdrawalStatus", "withdrawal_status")),
                to_str_or_none(first(record, "docInfoEditStatus", "doc_info_edit_status")),
                described["parent_doc_id"],
                to_str_or_none(first(record, "opeDateTime", "operation_datetime")),
                described["submit_datetime"],
                described["doc_description"],
                date_iso(first(record, "periodStart", "period_start")),
                date_iso(first(record, "periodEnd", "period_end")),
                to_str_or_none(first(record, "edinetCode", "edinet_code")),
                to_str_or_none(first(record, "issuerEdinetCode", "issuer_edinet_code")),
                to_str_or_none(first(record, "subjectEdinetCode", "subject_edinet_code")),
            )
        )
    return rows


def _edinet_metric_rows(
    asof_date: str,
    records: Iterable[Mapping[str, Any]],
) -> list[tuple[Any, ...]]:
    rows: list[tuple[Any, ...]] = []
    for record in records:
        ticker = normalize_ticker_or_none(first(record, "secCode", "ticker", "code", "Code"))
        if ticker is None:
            continue
        extractor_revision = to_str_or_none(first(record, "extractor_revision"))
        if extractor_revision is None:
            raise ValueError(f"EDINET metric row {ticker} is missing extractor_revision")
        raw_investment_securities = first(record, "investment_securities", "InvestmentSecurities")
        investment_securities = to_float(raw_investment_securities)
        if raw_investment_securities is not None and (
            investment_securities is None
            or not isfinite(investment_securities)
            or investment_securities < 0
        ):
            raise ValueError(f"EDINET metric row {ticker} has invalid investment_securities")
        rows.append(
            (
                asof_date,
                ticker,
                to_float(first(record, "sales_ttm", "SalesTTM")),
                to_float(first(record, "ocf_ttm", "OperatingCashFlowTTM")),
                to_float(first(record, "debt", "Debt")),
                to_float(first(record, "cash", "Cash")),
                to_float(first(record, "ebitda_ttm", "EBITDATTM")),
                to_str_or_none(first(record, "consolidation_basis", "ConsolidationBasis")),
                to_str_or_none(first(record, "ttm_quality_ev_ebitda", "TTMQualityEvEbitda")),
                to_str_or_none(first(record, "ttm_quality_p_s", "TTMQualityPS")),
                to_str_or_none(first(record, "ttm_quality_pcfr", "TTMQualityPCFR")),
                to_float(first(record, "operating_profit_ttm")),
                to_float(first(record, "depreciation_and_amortization_ttm")),
                to_float(first(record, "capex_ttm")),
                to_float(first(record, "fcf_ttm")),
                to_float(first(record, "net_cash")),
                to_float(first(record, "equity")),
                to_float(first(record, "total_assets")),
                to_str_or_none(first(record, "ttm_quality_fcf")),
                to_str_or_none(first(record, "ttm_quality_net_cash")),
                to_str_or_none(first(record, "source_doc_id")),
                to_str_or_none(first(record, "document_type")),
                to_str_or_none(first(record, "source_submit_datetime")),
                to_str_or_none(first(record, "source_period_start")),
                to_str_or_none(first(record, "source_period_end")),
                to_str_or_none(first(record, "capex_source")),
                json.dumps(first(record, "failure_reasons") or (), ensure_ascii=False),
                extractor_revision,
                to_str_or_none(first(record, "source_document_revision")),
                investment_securities,
            )
        )
    return rows
