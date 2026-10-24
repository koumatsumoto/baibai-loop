"""EDINET metricの取得・抽出・保存。原CSVから抽出意味revision付きで保存する。"""

from __future__ import annotations

import json
import sqlite3
import sys
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Protocol, TextIO

from baibai_engine.collection.edinet.models import EdinetAcquisitionError
from baibai_engine.market.edinet_metrics.csv import parse_csv_zip_metric_record
from baibai_engine.market.edinet_metrics.planning import (
    MetricExtractionPlan,
    can_reuse_edinet_metric,
    make_metric_extraction_plan,
)
from baibai_engine.market.edinet_metrics.revision import (
    DEFERRED_CSV_REASON,
    EDINET_METRIC_REVISION,
    PARTIAL_CSV_COVERAGE,
    has_hard_metric_failure,
)
from baibai_engine.market.edinet_metrics.store import (
    EDINETMetricBaselineError,
    read_edinet_metric_baseline,
)
from baibai_engine.market.edinet_metrics.write import store_edinet_metrics
from baibai_engine.market.edinet_models import EdinetMetricRecord, EDINETProviderError
from baibai_engine.market.edinet_selection import select_document_candidates
from baibai_engine.storage.observations.readiness import unreadable_store_reason
from baibai_engine.storage.observations.schema import SQLiteSchemaError


class EdinetMetricReader(Protocol):
    """Stored document and archive inputs for offline metric extraction."""

    def list_documents(self, on_date: date) -> list[dict[str, Any]]: ...

    def read_csv_zip(self, doc_id: str) -> bytes: ...


class EdinetMetricAcquisition(Protocol):
    def list_documents(self, on_date: date) -> list[dict[str, Any]]: ...

    def download_csv_zip(self, doc_id: str) -> bytes: ...


def refresh_edinet_metrics_command(
    *,
    asof_date: date,
    lookback_days: int,
    reader: EdinetMetricReader,
    acquisition: EdinetMetricAcquisition,
    sqlite_path: Path,
    stdout: TextIO | None = None,
) -> int:
    out = stdout if stdout is not None else sys.stdout
    try:
        cursor = asof_date - timedelta(days=lookback_days)
        while cursor <= asof_date:
            acquisition.list_documents(cursor)
            cursor += timedelta(days=1)
    except (
        EdinetAcquisitionError,
        EDINETProviderError,
        SQLiteSchemaError,
        sqlite3.Error,
        OSError,
        ValueError,
    ) as exc:
        print(f"EDINET source preparation failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    plan = prepare_edinet_metric_plan(
        asof_date=asof_date,
        lookback_days=lookback_days,
        reader=reader,
        sqlite_path=sqlite_path,
        stdout=out,
    )
    if plan is None:
        return 1
    try:
        for candidate in plan.archive_candidates:
            acquisition.download_csv_zip(candidate.doc_id)
    except (
        EdinetAcquisitionError,
        EDINETProviderError,
        SQLiteSchemaError,
        sqlite3.Error,
        OSError,
        ValueError,
    ) as exc:
        print(f"EDINET archive acquisition failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    return extract_edinet_metrics_from_plan(
        asof_date=asof_date,
        plan=plan,
        reader=reader,
        sqlite_path=sqlite_path,
        stdout=out,
    )


def extract_edinet_metrics_command(
    *,
    asof_date: date,
    lookback_days: int,
    reader: EdinetMetricReader,
    sqlite_path: Path,
    stdout: TextIO | None = None,
) -> int:
    out = stdout if stdout is not None else sys.stdout
    plan = prepare_edinet_metric_plan(
        asof_date=asof_date,
        lookback_days=lookback_days,
        reader=reader,
        sqlite_path=sqlite_path,
        stdout=out,
    )
    if plan is None:
        return 1
    return extract_edinet_metrics_from_plan(
        asof_date=asof_date,
        plan=plan,
        reader=reader,
        sqlite_path=sqlite_path,
        stdout=out,
    )


def prepare_edinet_metric_plan(
    *,
    asof_date: date,
    lookback_days: int,
    reader: EdinetMetricReader,
    sqlite_path: Path,
    stdout: TextIO | None = None,
) -> MetricExtractionPlan | None:
    out = stdout if stdout is not None else sys.stdout
    start = asof_date - timedelta(days=lookback_days)
    documents: list[dict[str, Any]] = []
    cursor = start
    listed_days = 0
    print(
        "listing EDINET documents "
        f"{start.isoformat()}..{asof_date.isoformat()} ({lookback_days + 1} day(s))",
        file=out,
        flush=True,
    )
    try:
        while cursor <= asof_date:
            documents.extend(reader.list_documents(cursor))
            cursor += timedelta(days=1)
            listed_days += 1
            if listed_days % 30 == 0 or cursor > asof_date:
                print(
                    f"listed EDINET documents: {listed_days}/{lookback_days + 1} day(s), "
                    f"{len(documents)} document(s)",
                    file=out,
                    flush=True,
                )
    except (EDINETProviderError, SQLiteSchemaError, sqlite3.Error, OSError, ValueError) as exc:
        message = f"EDINET document listing failed: {type(exc).__name__}: {exc}"
        _record_edinet_extraction_failure(sqlite_path, asof_date, message)
        print(message, file=sys.stderr)
        return None

    try:
        selection = select_document_candidates(documents, origin_start=start)
    except (EDINETProviderError, SQLiteSchemaError, sqlite3.Error, OSError, ValueError) as exc:
        message = f"EDINET document selection failed: {type(exc).__name__}: {exc}"
        _record_edinet_extraction_failure(sqlite_path, asof_date, message)
        print(message, file=sys.stderr)
        return None
    candidates = selection.candidates
    if not candidates:
        message = (
            f"no EDINET filings selected for --asof {asof_date.isoformat()} "
            f"within {start.isoformat()}..{asof_date.isoformat()}"
        )
        _record_edinet_extraction_failure(sqlite_path, asof_date, message)
        print(message, file=sys.stderr)
        return None
    print(
        f"selected {len(candidates)} EDINET filing(s) from {len(documents)} document(s)",
        file=out,
        flush=True,
    )
    extractor_revision = EDINET_METRIC_REVISION
    try:
        baseline = read_edinet_metric_baseline(sqlite_path, asof_date)
    except (
        EDINETMetricBaselineError,
        SQLiteSchemaError,
        sqlite3.Error,
        OSError,
        ValueError,
    ) as exc:
        print(f"EDINET metric baseline is corrupt: {exc}", file=sys.stderr)
        return None
    return make_metric_extraction_plan(
        selection=selection,
        baseline=baseline,
        extractor_revision=extractor_revision,
    )


def extract_edinet_metrics_from_plan(
    *,
    asof_date: date,
    plan: MetricExtractionPlan,
    reader: EdinetMetricReader,
    sqlite_path: Path,
    stdout: TextIO | None = None,
    unavailable_doc_ids: frozenset[str] = frozenset(),
) -> int:
    out = stdout if stdout is not None else sys.stdout
    candidates = plan.selection.candidates
    if not unavailable_doc_ids <= {item.doc_id for item in plan.archive_candidates}:
        raise ValueError("deferred CSV IDs must be selected, unreused archive inputs")
    quarantined_events = plan.selection.quarantined_events
    baseline = plan.baseline
    extractor_revision = plan.extractor_revision
    events_by_ticker = plan.events_by_ticker
    quarantined_tickers = set(events_by_ticker)
    quarantine_summary_sample = (
        ",".join(
            f"{event.doc_id}:{event.event_type}:{event.document_type or 'unknown'}"
            for event in quarantined_events[:5]
        )
        or "none"
    )
    if quarantined_events:
        sample = ", ".join(
            f"{event.doc_id}({event.event_type},type={event.document_type or 'unknown'},"
            f"ticker={event.ticker or 'unknown'})"
            for event in quarantined_events[:5]
        )
        print(
            "EDINET event quarantine: "
            f"events={len(quarantined_events)} affected_tickers={len(quarantined_tickers)}; "
            f"sample={sample}",
            file=sys.stderr,
            flush=True,
        )
    baseline_asof = baseline.asof_date.isoformat() if baseline is not None else "none"
    full_rebuild_reason = "no_baseline" if baseline is None else "none"
    if (
        baseline is not None
        and baseline.rows
        and all(row.extractor_revision != extractor_revision for row in baseline.rows.values())
    ):
        full_rebuild_reason = "incompatible_revision"
    records: list[EdinetMetricRecord] = []
    reused_count = 0
    parsed_count = 0
    hard_failure_count = 0
    quality_issue_count = 0
    for ticker in sorted(quarantined_tickers):
        candidate = candidates.get(ticker)
        baseline_record = (
            baseline.rows[ticker].record
            if baseline is not None and ticker in baseline.rows
            else None
        )
        events = tuple(
            sorted(
                events_by_ticker[ticker],
                key=lambda event: (event.doc_id, event.event_type, event.target_doc_id or ""),
            )
        )
        first_event = events[0]
        records.append(
            EdinetMetricRecord(
                ticker=ticker,
                source_doc_id=first_event.target_doc_id or first_event.doc_id,
                document_type=(
                    candidate.doc_type_code
                    if candidate is not None
                    else baseline_record.document_type
                    if baseline_record is not None
                    else first_event.document_type
                ),
                source_submit_datetime=(
                    candidate.submit_datetime
                    if candidate is not None
                    else baseline_record.source_submit_datetime
                    if baseline_record is not None
                    else None
                ),
                source_period_start=(
                    candidate.period_start
                    if candidate is not None
                    else baseline_record.source_period_start
                    if baseline_record is not None
                    else None
                ),
                source_period_end=(
                    candidate.period_end
                    if candidate is not None
                    else baseline_record.source_period_end
                    if baseline_record is not None
                    else None
                ),
                failure_reasons=tuple(
                    f"document_event_quarantined:{event.event_type}:{event.doc_id}"
                    for event in events
                ),
            )
        )
        quality_issue_count += 1
    processable_count = len(candidates) - len(quarantined_tickers & candidates.keys())
    processed_count = 0
    for candidate in sorted(candidates.values(), key=lambda item: item.ticker):
        if candidate.ticker in quarantined_tickers:
            continue
        processed_count += 1
        baseline_row = baseline.rows.get(candidate.ticker) if baseline is not None else None
        if baseline_row is not None and can_reuse_edinet_metric(
            candidate=candidate,
            baseline_row=baseline_row,
            extractor_revision=extractor_revision,
        ):
            record = baseline_row.record
            reused_count += 1
            if record.failure_reasons:
                quality_issue_count += 1
            records.append(record)
            continue
        if candidate.doc_id in unavailable_doc_ids:
            records.append(
                EdinetMetricRecord(
                    ticker=candidate.ticker,
                    source_doc_id=candidate.doc_id,
                    document_type=candidate.doc_type_code,
                    source_submit_datetime=candidate.submit_datetime,
                    source_period_start=candidate.period_start,
                    source_period_end=candidate.period_end,
                    failure_reasons=(DEFERRED_CSV_REASON,),
                )
            )
            continue
        parse_failed = False
        parsed_count += 1
        try:
            content = reader.read_csv_zip(candidate.doc_id)
            record = parse_csv_zip_metric_record(
                ticker=candidate.ticker,
                doc_id=candidate.doc_id,
                doc_type_code=candidate.doc_type_code,
                content=content,
                submit_datetime=candidate.submit_datetime,
                period_start=candidate.period_start,
                period_end=candidate.period_end,
            )
        except (EDINETProviderError, SQLiteSchemaError, sqlite3.Error, OSError, ValueError) as exc:
            hard_failure_count += 1
            error_text = str(exc).replace("\n", " ").replace("\r", " ")[:160]
            record = EdinetMetricRecord(
                ticker=candidate.ticker,
                source_doc_id=candidate.doc_id,
                document_type=candidate.doc_type_code,
                source_submit_datetime=candidate.submit_datetime,
                source_period_start=candidate.period_start,
                source_period_end=candidate.period_end,
                failure_reasons=(f"csv_parse_failed:{type(exc).__name__}:{error_text}",),
            )
            parse_failed = True
        if has_hard_metric_failure(record.failure_reasons) and not parse_failed:
            hard_failure_count += 1
            parse_failed = True
        if record.failure_reasons and not parse_failed:
            quality_issue_count += 1
        records.append(record)
        if processed_count % 50 == 0 or processed_count == processable_count:
            print(
                f"parsed EDINET CSV metrics: {processed_count}/{processable_count} filing(s); "
                f"{hard_failure_count} hard failure(s)",
                file=out,
                flush=True,
            )

    if hard_failure_count:
        _record_edinet_extraction_failure(
            sqlite_path,
            asof_date,
            f"{hard_failure_count} EDINET CSV hard failures",
        )
        print(
            "EDINET extraction summary: "
            f"selected={len(candidates)} reused={reused_count} parsed={parsed_count} "
            f"baseline_asof={baseline_asof} full_rebuild_reason={full_rebuild_reason} "
            f"extractor_revision={extractor_revision} "
            f"quarantined_events={len(quarantined_events)} "
            f"quarantined_tickers={len(quarantined_tickers)} "
            f"quarantine_sample={quarantine_summary_sample}; "
            f"hard_failures={hard_failure_count} deferred_archives={len(unavailable_doc_ids)} "
            f"quality_issues={quality_issue_count}; output=not-written",
            file=out,
        )
        return 1

    source_revisions = {
        candidate.ticker: candidate.source_document_revision for candidate in candidates.values()
    }
    payload = [
        _metric_record_payload(
            record,
            extractor_revision=extractor_revision,
            source_document_revision=source_revisions.get(record.ticker),
        )
        for record in records
    ]
    store_edinet_metrics(
        sqlite_path,
        asof_date,
        payload,
        status="partial" if unavailable_doc_ids else "ok",
        error=json.dumps(
            {"reason": PARTIAL_CSV_COVERAGE, "doc_ids": sorted(unavailable_doc_ids)}
        ) if unavailable_doc_ids else None,
    )
    print(
        "EDINET extraction summary: "
        f"selected={len(candidates)} reused={reused_count} parsed={parsed_count} "
        f"baseline_asof={baseline_asof} full_rebuild_reason={full_rebuild_reason} "
        f"extractor_revision={extractor_revision} "
        f"quarantined_events={len(quarantined_events)} "
        f"quarantined_tickers={len(quarantined_tickers)} "
        f"quarantine_sample={quarantine_summary_sample}; "
        f"hard_failures={hard_failure_count} deferred_archives={len(unavailable_doc_ids)} "
        f"quality_issues={quality_issue_count}; output={sqlite_path}",
        file=out,
    )
    return 0


def _metric_record_payload(
    record: EdinetMetricRecord,
    *,
    extractor_revision: str,
    source_document_revision: str | None,
) -> dict[str, object]:
    return {
        "ticker": record.ticker,
        "sales_ttm": record.sales_ttm,
        "ocf_ttm": record.ocf_ttm,
        "debt": record.debt,
        "cash": record.cash,
        "investment_securities": record.investment_securities,
        "ebitda_ttm": record.ebitda_ttm,
        "consolidation_basis": record.consolidation_basis,
        "ttm_quality_ev_ebitda": record.ttm_quality_ev_ebitda.value
        if record.ttm_quality_ev_ebitda is not None
        else None,
        "ttm_quality_p_s": record.ttm_quality_p_s.value,
        "ttm_quality_pcfr": record.ttm_quality_pcfr.value,
        "operating_profit_ttm": record.operating_profit_ttm,
        "depreciation_and_amortization_ttm": record.depreciation_and_amortization_ttm,
        "capex_ttm": record.capex_ttm,
        "ocf_receivables_cash_effect": record.ocf_receivables_cash_effect,
        "ocf_inventories_cash_effect": record.ocf_inventories_cash_effect,
        "ocf_payables_cash_effect": record.ocf_payables_cash_effect,
        "ocf_contract_liabilities_cash_effect": record.ocf_contract_liabilities_cash_effect,
        "ocf_advances_received_cash_effect": record.ocf_advances_received_cash_effect,
        "ocf_other_payables_cash_effect": record.ocf_other_payables_cash_effect,
        "capex_ppe_reported": record.capex_ppe_reported,
        "capex_intangible_reported": record.capex_intangible_reported,
        "fcf_ttm": record.fcf_ttm,
        "net_cash": record.net_cash,
        "equity": record.equity,
        "total_assets": record.total_assets,
        "ttm_quality_fcf": record.ttm_quality_fcf.value
        if record.ttm_quality_fcf is not None
        else None,
        "ttm_quality_net_cash": record.ttm_quality_net_cash.value,
        "source_doc_id": record.source_doc_id,
        "document_type": record.document_type,
        "source_submit_datetime": record.source_submit_datetime,
        "source_period_start": _date_iso(record.source_period_start),
        "source_period_end": _date_iso(record.source_period_end),
        "capex_source": record.capex_source,
        "failure_reasons": list(record.failure_reasons),
        "extractor_revision": extractor_revision,
        "source_document_revision": source_document_revision,
    }


def _record_edinet_extraction_failure(
    sqlite_path: Path,
    asof_date: date,
    message: str,
) -> None:
    """Record a failed attempt without destroying a usable target-day snapshot.

    Recording the failure deletes the day's rows, so the check for an existing
    snapshot is what stands between a failed attempt and the loss of a good one.
    If an existing store is unreadable or its baseline is inconsistent, the check
    cannot establish that replacement is safe, so nothing is written. This path
    does not migrate a store. An absent store has no snapshot to preserve.
    """
    if sqlite_path.exists() and unreadable_store_reason(sqlite_path) is not None:
        return
    try:
        baseline = read_edinet_metric_baseline(sqlite_path, asof_date)
    except (EDINETMetricBaselineError, SQLiteSchemaError, sqlite3.Error, OSError, ValueError):
        return
    if baseline is not None and baseline.asof_date == asof_date:
        return
    store_edinet_metrics(
        sqlite_path,
        asof_date,
        [],
        status="failed",
        error=message,
    )


def _date_iso(value: date | None) -> str | None:
    return value.isoformat() if value is not None else None
