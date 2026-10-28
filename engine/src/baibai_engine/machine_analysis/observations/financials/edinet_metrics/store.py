"""EDINET開示・抽出baselineの読取とcoverage/count/品質の整合検査。"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from datetime import date
from pathlib import Path
from typing import Any

from baibai_engine.machine_analysis.observations.financials.edinet_decode import normalize_metric_record
from baibai_engine.machine_analysis.observations.disclosures.edinet_document_store import read_stored_documents
from baibai_engine.machine_analysis.observations.financials.edinet_metrics.revision import (
    DEFERRED_CSV_REASON,
    PARTIAL_CSV_COVERAGE,
    has_hard_metric_failure,
)
from baibai_engine.machine_analysis.observations.financials.edinet_models import EdinetMetricRecord, EDINETProviderError
from baibai_engine.storage.sqlite.source_cache.coverage import date_covered
from baibai_engine.storage.sqlite.source_cache.schema import connect_current


class EDINETMetricBaselineError(RuntimeError):
    """Raised when the newest eligible baseline claims an inconsistent snapshot."""


@dataclass(frozen=True, slots=True)
class EDINETMetricBaselineRow:
    record: EdinetMetricRecord
    extractor_revision: str | None
    source_document_revision: str | None


@dataclass(frozen=True, slots=True)
class EDINETMetricBaseline:
    asof_date: date
    rows: Mapping[str, EDINETMetricBaselineRow]
    recorded_at_utc: str | None = None


_METRIC_SELECT = (
    "SELECT ticker, sales_ttm, ocf_ttm, debt, cash, ebitda_ttm, consolidation_basis, "
    "ttm_quality_ev_ebitda, ttm_quality_p_s, ttm_quality_pcfr, operating_profit_ttm, "
    "depreciation_and_amortization_ttm, capex_ttm, fcf_ttm, net_cash, equity, "
    "total_assets, ttm_quality_fcf, ttm_quality_net_cash, source_doc_id, "
    "document_type, source_submit_datetime, source_period_start, source_period_end, "
    "capex_source, failure_reasons, investment_securities, "
    "ocf_receivables_cash_effect, ocf_inventories_cash_effect, "
    "ocf_payables_cash_effect, ocf_contract_liabilities_cash_effect, "
    "ocf_advances_received_cash_effect, ocf_other_payables_cash_effect, "
    "capex_ppe_reported, capex_intangible_reported"
)


def _normalize_edinet_metric_sql_row(row: sqlite3.Row) -> EdinetMetricRecord:
    """Decode named SQL columns without a parallel positional field mapping."""
    payload = dict(row)
    reasons = json.loads(row["failure_reasons"]) if row["failure_reasons"] else []
    if not isinstance(reasons, list) or any(not isinstance(reason, str) for reason in reasons):
        raise ValueError("EDINET metric failure_reasons must be a JSON string list")
    payload["failure_reasons"] = reasons
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
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("BEGIN")
        coverage_rows = conn.execute(
            "SELECT coverage_key, coverage_start, coverage_end, record_count, status, error, "
            "fetched_at_utc "
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
            fetched_at_utc,
        ) in coverage_rows:
            key = str(coverage_key)
            if status == "failed":
                continue
            if status not in {"ok", "partial"}:
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
                or (status == "ok" and error is not None)
                or not isinstance(record_count, int)
                or record_count <= 0
            ):
                raise EDINETMetricBaselineError(
                    f"EDINET metric baseline coverage is inconsistent for {key}"
                )
            rows = conn.execute(
                _METRIC_SELECT + ", extractor_revision, source_document_revision "
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
                        extractor_revision=(
                            str(row["extractor_revision"])
                            if row["extractor_revision"] is not None
                            else None
                        ),
                        source_document_revision=(
                            str(row["source_document_revision"])
                            if row["source_document_revision"] is not None
                            else None
                        ),
                    )
            except (TypeError, ValueError, EDINETProviderError) as exc:
                raise EDINETMetricBaselineError(
                    f"EDINET metric baseline row is invalid for {key}: {type(exc).__name__}"
                ) from exc
            if len(baseline_rows) != record_count:
                raise EDINETMetricBaselineError(
                    f"EDINET metric baseline ticker identity mismatch for {key}"
                )
            _validate_deferred_coverage(baseline_rows, status=status, error=error, key=key)
            return EDINETMetricBaseline(
                asof_date=baseline_asof, rows=baseline_rows, recorded_at_utc=fetched_at_utc
            )
    except sqlite3.OperationalError as exc:
        raise EDINETMetricBaselineError(f"EDINET metric baseline read failed: {exc}") from exc
    finally:
        conn.close()
    return None


def _validate_deferred_coverage(
    rows: Mapping[str, EDINETMetricBaselineRow], *, status: str, error: str | None, key: str
) -> None:
    """Accept only explicit missing-archive partials, never arbitrary failed writes."""
    missing: set[str] = set()
    for row in rows.values():
        record = row.record
        if DEFERRED_CSV_REASON not in record.failure_reasons:
            continue
        if (
            record.failure_reasons != (DEFERRED_CSV_REASON,)
            or record.source_doc_id is None
            or any(isinstance(value, int | float) for value in asdict(record).values())
        ):
            raise EDINETMetricBaselineError(f"invalid deferred EDINET metric row for {key}")
        missing.add(record.source_doc_id)
    if status == "ok":
        if missing:
            raise EDINETMetricBaselineError(f"EDINET missing archives claimed complete for {key}")
        return
    try:
        details = json.loads(error or "null")
        ids = details.get("doc_ids") if isinstance(details, dict) else None
        valid = (
            isinstance(details, dict)
            and details.get("reason") == PARTIAL_CSV_COVERAGE
            and isinstance(ids, list)
            and bool(ids)
            and all(isinstance(item, str) for item in ids)
            and len(set(ids)) == len(ids)
            and set(ids) == missing
        )
    except (TypeError, ValueError) as exc:
        raise EDINETMetricBaselineError(f"EDINET partial coverage is invalid for {key}") from exc
    if not valid:
        raise EDINETMetricBaselineError(f"EDINET partial coverage is inconsistent for {key}")


def read_edinet_documents(sqlite_path: Path, on_date: date) -> list[dict[str, Any]] | None:
    """Read retained origins and source-key-preserving operations for one day.

    Missing coverage returns None; database query failures propagate.
    """
    if not sqlite_path.exists():
        return None
    conn = connect_current(sqlite_path)
    if conn is None:
        return None
    try:
        conn.execute("BEGIN")
        if not date_covered(conn, "edinet_documents", on_date):
            return None
        return read_stored_documents(conn, through=on_date, start=on_date)
    finally:
        conn.close()


def read_unrechecked_edinet_document_dates(sqlite_path: Path, *, before: date) -> tuple[date, ...]:
    """Return document-list dates without proof of a later source recheck."""
    if not sqlite_path.exists():
        return ()
    conn = connect_current(sqlite_path)
    if conn is None:
        return ()
    try:
        rows = conn.execute(
            "SELECT doc_date FROM edinet_document_lists "
            "WHERE rechecked_asof IS NULL AND doc_date < ? ORDER BY doc_date",
            (before.isoformat(),),
        ).fetchall()
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
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("BEGIN")
        if not date_covered(conn, "edinet_metrics", asof_date):
            return None
        rows = conn.execute(
            _METRIC_SELECT + " FROM edinet_metrics WHERE asof_date = ?",
            (asof_date.isoformat(),),
        ).fetchall()
    finally:
        conn.close()

    records: dict[str, EdinetMetricRecord] = {}
    for row in rows:
        record = _normalize_edinet_metric_sql_row(row)
        records[record.ticker] = record
    return records
