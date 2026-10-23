"""Transactional replacement of extracted EDINET metrics and coverage."""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from datetime import date
from math import isfinite
from pathlib import Path
from typing import Any

from baibai_engine.market.normalization import (
    normalize_ticker_or_none,
    to_float,
    to_str_or_none,
)
from baibai_engine.storage.observations.coverage import (
    date_range_row_count,
    delete_overlapping_source_coverage,
    record_source_coverage,
)
from baibai_engine.storage.observations.schema import open_connection


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
                "extractor_revision, source_document_revision, investment_securities, "
                "ocf_receivables_cash_effect, ocf_inventories_cash_effect, "
                "ocf_payables_cash_effect, ocf_contract_liabilities_cash_effect, "
                "ocf_advances_received_cash_effect, ocf_other_payables_cash_effect, "
                "capex_ppe_reported, capex_intangible_reported"
                ") VALUES (" + ", ".join("?" for _ in range(38)) + ")",
                rows,
            )
        persisted_count = date_range_row_count(
            conn, "edinet_metrics", "asof_date", asof_date, asof_date
        )
        record_source_coverage(
            conn,
            source="edinet_metrics",
            coverage_key=asof_date.isoformat(),
            coverage_start=asof_date.isoformat(),
            coverage_end=asof_date.isoformat(),
            record_count=persisted_count,
            status=stored_status,
            error=stored_error,
        )
        conn.commit()
        return persisted_count
    finally:
        conn.close()


def _edinet_metric_rows(
    asof_date: str,
    records: Iterable[Mapping[str, Any]],
) -> list[tuple[Any, ...]]:
    rows: list[tuple[Any, ...]] = []
    for record in records:
        ticker = normalize_ticker_or_none(record.get("ticker"))
        if ticker is None:
            continue
        extractor_revision = to_str_or_none(record.get("extractor_revision"))
        if extractor_revision is None:
            raise ValueError(f"EDINET metric row {ticker} is missing extractor_revision")
        raw_investment_securities = record.get("investment_securities")
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
                to_float(record.get("sales_ttm")),
                to_float(record.get("ocf_ttm")),
                to_float(record.get("debt")),
                to_float(record.get("cash")),
                to_float(record.get("ebitda_ttm")),
                to_str_or_none(record.get("consolidation_basis")),
                to_str_or_none(record.get("ttm_quality_ev_ebitda")),
                to_str_or_none(record.get("ttm_quality_p_s")),
                to_str_or_none(record.get("ttm_quality_pcfr")),
                to_float(record.get("operating_profit_ttm")),
                to_float(record.get("depreciation_and_amortization_ttm")),
                to_float(record.get("capex_ttm")),
                to_float(record.get("fcf_ttm")),
                to_float(record.get("net_cash")),
                to_float(record.get("equity")),
                to_float(record.get("total_assets")),
                to_str_or_none(record.get("ttm_quality_fcf")),
                to_str_or_none(record.get("ttm_quality_net_cash")),
                to_str_or_none(record.get("source_doc_id")),
                to_str_or_none(record.get("document_type")),
                to_str_or_none(record.get("source_submit_datetime")),
                to_str_or_none(record.get("source_period_start")),
                to_str_or_none(record.get("source_period_end")),
                to_str_or_none(record.get("capex_source")),
                json.dumps(record.get("failure_reasons") or (), ensure_ascii=False),
                extractor_revision,
                to_str_or_none(record.get("source_document_revision")),
                investment_securities,
                to_float(record.get("ocf_receivables_cash_effect")),
                to_float(record.get("ocf_inventories_cash_effect")),
                to_float(record.get("ocf_payables_cash_effect")),
                to_float(record.get("ocf_contract_liabilities_cash_effect")),
                to_float(record.get("ocf_advances_received_cash_effect")),
                to_float(record.get("ocf_other_payables_cash_effect")),
                to_float(record.get("capex_ppe_reported")),
                to_float(record.get("capex_intangible_reported")),
            )
        )
    return rows
