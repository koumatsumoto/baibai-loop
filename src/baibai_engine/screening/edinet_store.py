"""Reads of the EDINET slice of the market store.

These are the only store reads the EDINET extraction performs, and the extractor's
revision manifest is the import closure of that extraction. Leaving them in
`sqlite_reader`, which also reads master snapshots, fin summaries, JPX regulations and
weekly margin, would put all of those into the manifest — so an edit to any of them
would discard every reusable EDINET metric row and force a full re-download of several
thousand filings.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

from baibai_engine.market.sqlite import connect_current

from .edinet_revision import has_hard_metric_failure
from .providers.edinet import EdinetMetricRecord, normalize_metric_record
from .source_coverage import date_covered


class EDINETMetricBaselineError(RuntimeError):
    """Raised when the newest successful baseline claims an inconsistent snapshot."""


@dataclass(frozen=True, slots=True)
class EDINETMetricBaselineRow:
    record: EdinetMetricRecord
    extractor_revision: str | None
    source_document_revision: str | None


@dataclass(frozen=True, slots=True)
class EDINETMetricBaseline:
    asof_date: date
    rows: Mapping[str, EDINETMetricBaselineRow]


def _normalize_edinet_metric_sql_row(row: Sequence[Any]) -> EdinetMetricRecord:
    """Reuse the provider's normalize step so SQLite rows get the same coercion.

    The typed columns already match the normalizer's key set, so the payload it
    receives is the one it expects from the JSON path.
    """
    payload = {
        "ticker": row[0],
        "sales_ttm": row[1],
        "ocf_ttm": row[2],
        "debt": row[3],
        "cash": row[4],
        "ebitda_ttm": row[5],
        "consolidation_basis": row[6],
        "ttm_quality_ev_ebitda": row[7],
        "ttm_quality_p_s": row[8],
        "ttm_quality_pcfr": row[9],
        "operating_profit_ttm": row[10],
        "depreciation_and_amortization_ttm": row[11],
        "capex_ttm": row[12],
        "fcf_ttm": row[13],
        "net_cash": row[14],
        "equity": row[15],
        "total_assets": row[16],
        "ttm_quality_fcf": row[17],
        "ttm_quality_net_cash": row[18],
        "source_doc_id": row[19],
        "document_type": row[20],
        "source_submit_datetime": row[21],
        "source_period_start": row[22],
        "source_period_end": row[23],
        "capex_source": row[24],
        "failure_reasons": json.loads(row[25]) if row[25] else [],
        "investment_securities": row[26],
    }
    return normalize_metric_record(payload)


def read_edinet_metric_baseline(
    sqlite_path: Path,
    target_asof: date,
) -> EDINETMetricBaseline | None:
    """Read the newest eligible snapshot at or before ``target_asof``."""
    if not sqlite_path.exists():
        return None
    conn = connect_current(sqlite_path)
    if conn is None:
        return None
    try:
        coverage_rows = conn.execute(
            "SELECT coverage_key, coverage_start, coverage_end, record_count, status, error "
            "FROM source_coverage WHERE source = 'edinet_metrics' AND coverage_key <= ? "
            "ORDER BY coverage_key DESC",
            (target_asof.isoformat(),),
        ).fetchall()
        for (
            coverage_key,
            coverage_start,
            coverage_end,
            record_count,
            status,
            error,
        ) in coverage_rows:
            key = str(coverage_key)
            if status == "failed":
                continue
            if status != "ok":
                raise EDINETMetricBaselineError(
                    f"EDINET metric baseline {key} has unsupported status {status!r}"
                )
            try:
                baseline_asof = date.fromisoformat(key)
            except ValueError as exc:
                raise EDINETMetricBaselineError(
                    f"EDINET metric baseline has invalid coverage_key {key!r}"
                ) from exc
            if (
                baseline_asof > target_asof
                or coverage_start != key
                or coverage_end != key
                or error is not None
                or not isinstance(record_count, int)
                or record_count <= 0
            ):
                raise EDINETMetricBaselineError(
                    f"EDINET metric baseline coverage is inconsistent for {key}"
                )
            rows = conn.execute(
                "SELECT ticker, sales_ttm, ocf_ttm, debt, cash, ebitda_ttm, "
                "consolidation_basis, ttm_quality_ev_ebitda, ttm_quality_p_s, "
                "ttm_quality_pcfr, operating_profit_ttm, "
                "depreciation_and_amortization_ttm, capex_ttm, fcf_ttm, net_cash, "
                "equity, total_assets, ttm_quality_fcf, ttm_quality_net_cash, "
                "source_doc_id, document_type, source_submit_datetime, "
                "source_period_start, source_period_end, capex_source, failure_reasons, "
                "investment_securities, extractor_revision, source_document_revision "
                "FROM edinet_metrics WHERE asof_date = ? ORDER BY ticker",
                (key,),
            ).fetchall()
            if len(rows) != record_count:
                raise EDINETMetricBaselineError(
                    f"EDINET metric baseline row count mismatch for {key}: "
                    f"coverage={record_count} rows={len(rows)}"
                )
            baseline_rows: dict[str, EDINETMetricBaselineRow] = {}
            try:
                for row in rows:
                    record = _normalize_edinet_metric_sql_row(row)
                    if has_hard_metric_failure(record.failure_reasons):
                        raise EDINETMetricBaselineError(
                            f"EDINET metric baseline contains a hard parser failure for "
                            f"{key}/{record.ticker}"
                        )
                    baseline_rows[record.ticker] = EDINETMetricBaselineRow(
                        record=record,
                        extractor_revision=str(row[27]) if row[27] is not None else None,
                        source_document_revision=(str(row[28]) if row[28] is not None else None),
                    )
            except (TypeError, ValueError, json.JSONDecodeError) as exc:
                raise EDINETMetricBaselineError(
                    f"EDINET metric baseline row is invalid for {key}: {type(exc).__name__}"
                ) from exc
            if len(baseline_rows) != record_count:
                raise EDINETMetricBaselineError(
                    f"EDINET metric baseline ticker identity mismatch for {key}"
                )
            return EDINETMetricBaseline(asof_date=baseline_asof, rows=baseline_rows)
    except sqlite3.OperationalError as exc:
        raise EDINETMetricBaselineError(f"EDINET metric baseline read failed: {exc}") from exc
    finally:
        conn.close()
    return None


def read_edinet_documents(sqlite_path: Path, on_date: date) -> list[dict[str, Any]] | None:
    """Return raw EDINET document records for `on_date` from SQLite, or
    `None` if the cache cannot serve the date.
    """
    if not sqlite_path.exists():
        return None
    conn = connect_current(sqlite_path)
    if conn is None:
        return None
    try:
        if not date_covered(conn, "edinet_documents", on_date):
            return None
        rows = conn.execute(
            "SELECT sequence_number, doc_id, sec_code, doc_type_code, csv_flag, xbrl_flag, "
            "legal_status, disclosure_status, withdrawal_status, doc_info_edit_status, "
            "parent_doc_id, operation_datetime, submit_datetime, doc_description, "
            "period_start, period_end "
            "FROM edinet_documents WHERE doc_date = ? ORDER BY sequence_number",
            (on_date.isoformat(),),
        ).fetchall()
    except sqlite3.OperationalError:
        return None
    finally:
        conn.close()
    return [
        {
            "doc_date": on_date.isoformat(),
            "seqNumber": sequence_number,
            "docID": doc_id,
            "secCode": sec_code,
            "docTypeCode": doc_type_code,
            "csvFlag": csv_flag,
            "xbrlFlag": xbrl_flag,
            "legalStatus": legal_status,
            "disclosureStatus": disclosure_status,
            "withdrawalStatus": withdrawal_status,
            "docInfoEditStatus": doc_info_edit_status,
            "parentDocID": parent_doc_id,
            "opeDateTime": operation_datetime,
            "submitDateTime": submit_datetime,
            "docDescription": doc_description,
            "periodStart": period_start,
            "periodEnd": period_end,
        }
        for (
            sequence_number,
            doc_id,
            sec_code,
            doc_type_code,
            csv_flag,
            xbrl_flag,
            legal_status,
            disclosure_status,
            withdrawal_status,
            doc_info_edit_status,
            parent_doc_id,
            operation_datetime,
            submit_datetime,
            doc_description,
            period_start,
            period_end,
        ) in rows
    ]


def read_unfinalized_edinet_document_dates(sqlite_path: Path, *, before: date) -> tuple[date, ...]:
    """Return fetched EDINET file dates that still require a final refresh."""
    if not sqlite_path.exists():
        return ()
    conn = connect_current(sqlite_path)
    if conn is None:
        return ()
    try:
        rows = conn.execute(
            "SELECT doc_date FROM edinet_document_lists "
            "WHERE is_final = 0 AND doc_date < ? ORDER BY doc_date",
            (before.isoformat(),),
        ).fetchall()
    except sqlite3.OperationalError:
        return ()
    finally:
        conn.close()
    return tuple(date.fromisoformat(str(row[0])) for row in rows)


def read_edinet_metrics(
    sqlite_path: Path, asof_date: date
) -> Mapping[str, EdinetMetricRecord] | None:
    """Return EDINET metric records keyed by ticker for `asof_date`, or
    `None` if the cache cannot serve the date.
    """
    if not sqlite_path.exists():
        return None
    conn = connect_current(sqlite_path)
    if conn is None:
        return None
    try:
        if not date_covered(conn, "edinet_metrics", asof_date):
            return None
        rows = conn.execute(
            "SELECT ticker, sales_ttm, ocf_ttm, debt, cash, ebitda_ttm, "
            "consolidation_basis, ttm_quality_ev_ebitda, ttm_quality_p_s, ttm_quality_pcfr, "
            "operating_profit_ttm, depreciation_and_amortization_ttm, capex_ttm, fcf_ttm, "
            "net_cash, equity, total_assets, ttm_quality_fcf, ttm_quality_net_cash, "
            "source_doc_id, document_type, source_submit_datetime, source_period_start, "
            "source_period_end, capex_source, failure_reasons, investment_securities "
            "FROM edinet_metrics WHERE asof_date = ?",
            (asof_date.isoformat(),),
        ).fetchall()
    except sqlite3.OperationalError:
        return None
    finally:
        conn.close()

    # Reuse the provider's normalize step so SQLite-backed records pick up
    # the same TTMQuality coercion / shape as the JSON path. Pass an
    # already-normalized payload that the function expects (typed columns
    # already match its key set).
    records: dict[str, EdinetMetricRecord] = {}
    for row in rows:
        record = _normalize_edinet_metric_sql_row(row)
        records[record.ticker] = record
    return records
