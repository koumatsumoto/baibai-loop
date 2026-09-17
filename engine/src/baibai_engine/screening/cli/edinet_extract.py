"""The `extract-edinet-metrics` command — the entry point of the EDINET metric path.

The extractor's revision is the hash of the modules this file *references*, so what it
names decides when every stored EDINET metric row is discarded and several thousand
filings are downloaded again. It therefore lives on its own rather than beside the other
cache commands: `bootstrap-cache`, `verify-cache-coverage` and `backfill-history` name
J-Quants, JPX and coverage machinery that cannot change an EDINET metric row's value, and
a module shared with them would put all of it into the closure.

The same reasoning shapes the imports below. The store read comes from `edinet_store`
rather than `sqlite_reader`, the store write from `sqlite_cache.edinet` rather than the
package facade, and the provider is a protocol declared here rather than the CLI's shared
adapter bundle — each of those alternatives names the other sources.
"""

from __future__ import annotations

import sys
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Protocol, TextIO

from baibai_engine.screening.edinet_revision import (
    compute_extractor_revision,
    has_hard_metric_failure,
)
from baibai_engine.screening.edinet_store import (
    EDINETMetricBaselineError,
    EDINETMetricBaselineRow,
    read_edinet_metric_baseline,
)
from baibai_engine.screening.providers.edinet import (
    TARGET_DOC_TYPE_CODES,
    EdinetDocumentCandidate,
    EdinetMetricRecord,
    EDINETProviderError,
    EDINETRateLimitError,
    QuarantinedDocumentEvent,
    select_document_candidates,
)
from baibai_engine.screening.providers.edinet_csv import parse_csv_zip_metric_record
from baibai_engine.screening.sqlite_cache.edinet import store_edinet_metrics
from baibai_engine.screening.store_readiness import unreadable_store_reason

from .common import _date_iso


class EdinetExtractionSource(Protocol):
    """The two provider calls the extraction makes.

    Narrower than the CLI's shared `EDINETAdapter` on purpose: naming that one here
    would pull the whole adapter bundle — and with it J-Quants and JPX — into the
    revision manifest.
    """

    def list_documents(self, on_date: date) -> list[dict[str, Any]]: ...

    def download_csv_zip(self, doc_id: str) -> bytes: ...


def extract_edinet_metrics_command(
    *,
    asof_date: date,
    lookback_days: int,
    provider: EdinetExtractionSource,
    sqlite_path: Path,
    stdout: TextIO | None = None,
) -> int:
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
            documents.extend(provider.list_documents(cursor))
            cursor += timedelta(days=1)
            listed_days += 1
            if listed_days % 30 == 0 or cursor > asof_date:
                print(
                    f"listed EDINET documents: {listed_days}/{lookback_days + 1} day(s), "
                    f"{len(documents)} document(s)",
                    file=out,
                    flush=True,
                )
    except EDINETProviderError as exc:
        message = f"EDINET document listing failed: {type(exc).__name__}: {exc}"
        _record_edinet_extraction_failure(sqlite_path, asof_date, message)
        print(message, file=sys.stderr)
        return 1

    try:
        selection = select_document_candidates(documents, origin_start=start)
    except EDINETProviderError as exc:
        message = f"EDINET document selection failed: {type(exc).__name__}: {exc}"
        _record_edinet_extraction_failure(sqlite_path, asof_date, message)
        print(message, file=sys.stderr)
        return 1
    candidates = selection.candidates
    quarantined_events = selection.quarantined_events
    if not candidates:
        message = (
            f"no EDINET filings selected for --asof {asof_date.isoformat()} "
            f"within {start.isoformat()}..{asof_date.isoformat()}"
        )
        _record_edinet_extraction_failure(sqlite_path, asof_date, message)
        print(message, file=sys.stderr)
        return 1
    print(
        f"selected {len(candidates)} EDINET filing(s) from {len(documents)} document(s)",
        file=out,
        flush=True,
    )
    extractor_revision = compute_extractor_revision()
    try:
        baseline = read_edinet_metric_baseline(sqlite_path, asof_date)
    except EDINETMetricBaselineError as exc:
        print(f"EDINET metric baseline is corrupt: {exc}", file=sys.stderr)
        return 1
    events_by_ticker: dict[str, set[QuarantinedDocumentEvent]] = {}
    for event in quarantined_events:
        if event.ticker is not None and event.document_type in TARGET_DOC_TYPE_CODES:
            events_by_ticker.setdefault(event.ticker, set()).add(event)
    if baseline is not None:
        baseline_tickers_by_doc: dict[str, set[str]] = {}
        for ticker, row in baseline.rows.items():
            record = row.record
            if record.source_doc_id is not None and record.document_type in TARGET_DOC_TYPE_CODES:
                baseline_tickers_by_doc.setdefault(record.source_doc_id, set()).add(ticker)
        for event in quarantined_events:
            if event.target_doc_id is None:
                continue
            for ticker in baseline_tickers_by_doc.get(event.target_doc_id, ()):
                events_by_ticker.setdefault(ticker, set()).add(event)
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
    downloaded_count = 0
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
        if baseline_row is not None and _can_reuse_edinet_metric(
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
        parse_failed = False
        downloaded_count += 1
        try:
            content = provider.download_csv_zip(candidate.doc_id)
            record = parse_csv_zip_metric_record(
                ticker=candidate.ticker,
                doc_id=candidate.doc_id,
                doc_type_code=candidate.doc_type_code,
                content=content,
                submit_datetime=candidate.submit_datetime,
                period_start=candidate.period_start,
                period_end=candidate.period_end,
            )
        except EDINETRateLimitError as exc:
            message = f"EDINET CSV download rate limited: {type(exc).__name__}: {exc}"
            _record_edinet_extraction_failure(sqlite_path, asof_date, message)
            print(message, file=sys.stderr)
            return 1
        except (EDINETProviderError, OSError, ValueError) as exc:
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
            f"selected={len(candidates)} reused={reused_count} downloaded={downloaded_count} "
            f"baseline_asof={baseline_asof} full_rebuild_reason={full_rebuild_reason} "
            f"extractor_revision={extractor_revision} "
            f"quarantined_events={len(quarantined_events)} "
            f"quarantined_tickers={len(quarantined_tickers)} "
            f"quarantine_sample={quarantine_summary_sample}; "
            f"hard_failures={hard_failure_count} "
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
        status="ok",
        error=None,
    )
    print(
        "EDINET extraction summary: "
        f"selected={len(candidates)} reused={reused_count} downloaded={downloaded_count} "
        f"baseline_asof={baseline_asof} full_rebuild_reason={full_rebuild_reason} "
        f"extractor_revision={extractor_revision} "
        f"quarantined_events={len(quarantined_events)} "
        f"quarantined_tickers={len(quarantined_tickers)} "
        f"quarantine_sample={quarantine_summary_sample}; "
        f"hard_failures={hard_failure_count} "
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


def _can_reuse_edinet_metric(
    *,
    candidate: EdinetDocumentCandidate,
    baseline_row: EDINETMetricBaselineRow,
    extractor_revision: str,
) -> bool:
    record = baseline_row.record
    return (
        not any(
            reason.startswith("document_event_quarantined:") for reason in record.failure_reasons
        )
        and baseline_row.extractor_revision == extractor_revision
        and baseline_row.source_document_revision == candidate.source_document_revision
        and candidate.source_document_revision is not None
        and record.ticker == candidate.ticker
        and record.source_doc_id == candidate.doc_id
        and record.document_type == candidate.doc_type_code
        and record.source_submit_datetime == candidate.submit_datetime
        and record.source_period_start == candidate.period_start
        and record.source_period_end == candidate.period_end
    )


def _record_edinet_extraction_failure(
    sqlite_path: Path,
    asof_date: date,
    message: str,
) -> None:
    """Record a failed attempt without destroying a usable target-day snapshot.

    Recording the failure deletes the day's rows, so the check for an existing
    snapshot is what stands between a failed attempt and the loss of a good one.
    A store that exists but the readers cannot open answers "no snapshot" to that
    check while the write path below would migrate it and delete anyway, so there
    the question went unanswered and nothing is written. A store that does not
    exist yet holds nothing to lose, and the failure is recorded as usual.
    """
    if sqlite_path.exists() and unreadable_store_reason(sqlite_path) is not None:
        return
    try:
        baseline = read_edinet_metric_baseline(sqlite_path, asof_date)
    except EDINETMetricBaselineError:
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
