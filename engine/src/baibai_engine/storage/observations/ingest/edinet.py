"""EDINET ingest: documents and extracted metrics."""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from datetime import UTC, date
from math import isfinite
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from baibai_engine.market.edinet_list_projection import DocumentListProjection

from baibai_engine.storage.observations.convert import (
    date_iso,
    first,
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


def store_edinet_document_projection(
    db_path: Path, projection: DocumentListProjection, *, rechecked_asof: date | None
) -> int:
    """Replace one day's derived rows and coverage from a raw-receipt projection."""
    conn = open_connection(db_path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        doc_date = projection.doc_date
        doc_date_iso = doc_date.isoformat()
        rows = _edinet_document_rows(doc_date_iso, projection.documents)
        selected = projection.selected_receipt
        if selected is None:
            process_datetime = None
            result_count = 0
            response_json = None
            fetched_at = projection.latest_received_at
        else:
            payload = projection.native_payload
            if payload is None:
                raise ValueError("selected EDINET receipt has no native payload")
            metadata = payload["metadata"]
            process_datetime = to_str_or_none(metadata.get("processDateTime"))
            result_count = int(str(metadata["resultset"]["count"]))
            response_json = json.dumps(
                payload, ensure_ascii=False, separators=(",", ":"), allow_nan=False
            )
            fetched_at = selected.received_at
        latest_at_iso = projection.latest_received_at.astimezone(UTC).isoformat(
            timespec="microseconds"
        )
        fetched_at_iso = fetched_at.astimezone(UTC).isoformat(timespec="microseconds")
        successful = projection.error is None and selected is not None
        conn.execute("DELETE FROM edinet_documents WHERE doc_date = ?", (doc_date_iso,))
        delete_overlapping_source_coverage(conn, "edinet_documents", doc_date, doc_date)
        if rows:
            conn.executemany(
                "INSERT INTO edinet_documents("
                "doc_date, sequence_number, doc_id, sec_code, doc_type_code, csv_flag, "
                "xbrl_flag, legal_status, disclosure_status, withdrawal_status, "
                "doc_info_edit_status, parent_doc_id, operation_datetime, submit_datetime, "
                "doc_description, period_start, period_end, edinet_code, "
                "issuer_edinet_code, subject_edinet_code"
                ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                rows,
            )
        persisted_count = date_range_row_count(
            conn, "edinet_documents", "doc_date", doc_date, doc_date
        )
        record_source_coverage(
            conn,
            source="edinet_documents",
            coverage_key=doc_date_iso,
            coverage_start=doc_date_iso,
            coverage_end=doc_date_iso,
            record_count=persisted_count,
            status="ok" if successful else "failed",
            error=None if successful else projection.error or "no selected EDINET receipt",
            fetched_at_utc=latest_at_iso,
        )
        conn.execute(
            "INSERT OR REPLACE INTO edinet_document_lists("
            "doc_date, process_datetime, result_count, fetched_at_utc, is_final, "
            "response_json, selected_receipt_id, latest_received_at_utc, rechecked_asof"
            ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                doc_date_iso,
                process_datetime,
                result_count,
                fetched_at_iso,
                int(successful and rechecked_asof is not None),
                response_json,
                selected.receipt_id if selected is not None else None,
                latest_at_iso,
                rechecked_asof.isoformat() if successful and rechecked_asof is not None else None,
            ),
        )
        conn.commit()
        return persisted_count
    except BaseException:
        conn.rollback()
        raise
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


def _edinet_document_rows(
    doc_date: str,
    records: Iterable[Mapping[str, Any]],
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
        retained_values = {
            "sec_code": to_str_or_none(first(record, "secCode", "sec_code")),
            "doc_type_code": to_str_or_none(first(record, "docTypeCode", "doc_type_code")),
            "parent_doc_id": to_str_or_none(first(record, "parentDocID", "parent_doc_id")),
            "submit_datetime": to_str_or_none(first(record, "submitDateTime", "submit_datetime")),
            "doc_description": to_str_or_none(first(record, "docDescription", "doc_description")),
            "edinet_code": to_str_or_none(first(record, "edinetCode", "edinet_code")),
            "issuer_edinet_code": to_str_or_none(
                first(record, "issuerEdinetCode", "issuer_edinet_code")
            ),
            "subject_edinet_code": to_str_or_none(
                first(record, "subjectEdinetCode", "subject_edinet_code")
            ),
        }
        rows.append(
            (
                doc_date,
                sequence_number,
                doc_id,
                retained_values["sec_code"],
                retained_values["doc_type_code"],
                to_str_or_none(first(record, "csvFlag", "csv_flag")),
                to_str_or_none(first(record, "xbrlFlag", "xbrl_flag")),
                to_str_or_none(first(record, "legalStatus", "legal_status")),
                to_str_or_none(first(record, "disclosureStatus", "disclosure_status")),
                to_str_or_none(first(record, "withdrawalStatus", "withdrawal_status")),
                to_str_or_none(first(record, "docInfoEditStatus", "doc_info_edit_status")),
                retained_values["parent_doc_id"],
                to_str_or_none(first(record, "opeDateTime", "operation_datetime")),
                retained_values["submit_datetime"],
                retained_values["doc_description"],
                date_iso(first(record, "periodStart", "period_start")),
                date_iso(first(record, "periodEnd", "period_end")),
                retained_values["edinet_code"],
                retained_values["issuer_edinet_code"],
                retained_values["subject_edinet_code"],
            )
        )
    return rows


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
