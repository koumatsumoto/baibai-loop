"""EDINET ingest: documents and extracted metrics."""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from datetime import date
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
from baibai_engine.market.sqlite.schema import open_connection


def store_edinet_documents(
    db_path: Path,
    on_date: date,
    records: Iterable[Mapping[str, Any]],
) -> int:
    conn = open_connection(db_path)
    try:
        records_list = list(records)
        rows = _edinet_document_rows(on_date.isoformat(), records_list)
        conn.execute("DELETE FROM edinet_documents WHERE doc_date = ?", (on_date.isoformat(),))
        delete_overlapping_source_coverage(conn, "edinet_documents", on_date, on_date)
        if rows:
            conn.executemany(
                "INSERT OR REPLACE INTO edinet_documents("
                "doc_date, doc_id, sec_code, doc_type_code, csv_flag, xbrl_flag, "
                "legal_status, disclosure_status, withdrawal_status, submit_datetime, "
                "doc_description, period_start, period_end"
                ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
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
            skipped_record_count=len(records_list) - len(rows),
            status="partial" if len(rows) < len(records_list) else "ok",
            error=(
                f"{len(records_list) - len(rows)} EDINET document rows were skipped"
                if len(rows) < len(records_list)
                else None
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
                "source_period_start, source_period_end, capex_source, failure_reasons"
                ") VALUES ("
                "?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, "
                "?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?"
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


def _edinet_document_rows(
    doc_date: str,
    records: Iterable[Mapping[str, Any]],
) -> list[tuple[Any, ...]]:
    rows: list[tuple[Any, ...]] = []
    for record in records:
        doc_id = to_str_or_none(first(record, "docID", "doc_id"))
        if doc_id is None:
            continue
        rows.append(
            (
                doc_date,
                doc_id,
                to_str_or_none(first(record, "secCode", "sec_code")),
                to_str_or_none(first(record, "docTypeCode", "doc_type_code")),
                to_str_or_none(first(record, "csvFlag", "csv_flag")),
                to_str_or_none(first(record, "xbrlFlag", "xbrl_flag")),
                to_str_or_none(first(record, "legalStatus", "legal_status")),
                to_str_or_none(first(record, "disclosureStatus", "disclosure_status")),
                to_str_or_none(first(record, "withdrawalStatus", "withdrawal_status")),
                to_str_or_none(first(record, "submitDateTime", "submit_datetime")),
                to_str_or_none(first(record, "docDescription", "doc_description")),
                date_iso(first(record, "periodStart", "period_start")),
                date_iso(first(record, "periodEnd", "period_end")),
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
            )
        )
    return rows
