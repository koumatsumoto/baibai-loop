"""Determine extraction inputs from one document selection and metric baseline."""

from __future__ import annotations

from collections.abc import Mapping
from contextlib import closing
from dataclasses import dataclass
from datetime import date
from itertools import groupby
from pathlib import Path

from baibai_engine.data_collection.providers.edinet.document_events import (
    QuarantinedDocumentEvent,
)
from baibai_engine.data_collection.providers.edinet.fields import parse_sec_code, text_or_none
from baibai_engine.data_collection.providers.edinet.list_projection import project_document_list
from baibai_engine.data_collection.providers.edinet.models import EDINETProviderError
from baibai_engine.data_collection.providers.edinet.receipts import read_list_responses
from baibai_engine.data_collection.providers.edinet.selection import (
    TARGET_DOC_TYPE_CODES,
    DocumentSelection,
    EdinetDocumentCandidate,
)
from baibai_engine.machine_analysis.observations.financials.edinet_metrics.revision import (
    DEFERRED_CSV_REASON,
)
from baibai_engine.machine_analysis.observations.financials.edinet_metrics.store import (
    EDINETMetricBaseline,
    EDINETMetricBaselineRow,
)
from baibai_engine.storage.sqlite.source_cache.read import connect_read_only


@dataclass(frozen=True, slots=True)
class MetricExtractionPlan:
    selection: DocumentSelection
    baseline: EDINETMetricBaseline | None
    extractor_revision: str
    events_by_ticker: Mapping[str, frozenset[QuarantinedDocumentEvent]]

    @property
    def archive_candidates(self) -> tuple[EdinetDocumentCandidate, ...]:
        """Only filings whose bytes will be parsed, never quarantined or reused rows."""
        result = []
        for candidate in sorted(self.selection.candidates.values(), key=lambda item: item.ticker):
            if candidate.ticker in self.events_by_ticker:
                continue
            row = self.baseline.rows.get(candidate.ticker) if self.baseline is not None else None
            if row is None or not can_reuse_edinet_metric(
                candidate=candidate, baseline_row=row, extractor_revision=self.extractor_revision
            ):
                result.append(candidate)
        return tuple(result)


def read_retained_metric_document_tickers(
    sqlite_path: Path, *, selection: DocumentSelection, through: date
) -> dict[str, frozenset[str]]:
    """Identify unresolved targets from original receipts, never revive omitted filings.

    A refreshed day can omit a formerly observed origin while a later withdrawal
    names only its docID. Its old receipt still establishes the affected ticker,
    not present eligibility, financial values or a past extraction timestamp.
    Read history only for unresolved targets; the normal no-event path is free.
    """
    targets = {
        event.target_doc_id
        for event in selection.quarantined_events
        if event.target_doc_id is not None
    }
    if not targets or not sqlite_path.is_file():
        return {}
    tickers: dict[str, set[str]] = {}
    with closing(connect_read_only(sqlite_path)) as connection:
        connection.execute("BEGIN")
        days = connection.execute(
            "SELECT DISTINCT doc_date FROM edinet_list_responses "
            "WHERE doc_date <= ? ORDER BY doc_date",
            (through.isoformat(),),
        ).fetchall()
        for (day,) in days:
            receipts = read_list_responses(connection, doc_date=date.fromisoformat(day))
            ordered = sorted(receipts, key=lambda receipt: receipt.received_at)
            for _, group in groupby(ordered, key=lambda receipt: receipt.received_at):
                same_time = tuple(group)
                # Match the source projection: a conflicting same-time group
                # makes later ordering unknowable, rather than ordering by UUID.
                if len({receipt.body for receipt in same_time}) != 1:
                    break
                projection = project_document_list(same_time)
                for document in projection.documents:
                    doc_id = str(document["docID"])
                    if (
                        doc_id not in targets
                        or text_or_none(document.get("docTypeCode")) not in TARGET_DOC_TYPE_CODES
                    ):
                        continue
                    try:
                        ticker = parse_sec_code(document.get("secCode"))
                    except EDINETProviderError:
                        continue
                    tickers.setdefault(doc_id, set()).add(ticker)
    return {doc_id: frozenset(values) for doc_id, values in tickers.items()}


def make_metric_extraction_plan(
    *,
    selection: DocumentSelection,
    baseline: EDINETMetricBaseline | None,
    extractor_revision: str,
    retained_tickers_by_doc: Mapping[str, frozenset[str]] | None = None,
) -> MetricExtractionPlan:
    events: dict[str, set[QuarantinedDocumentEvent]] = {}
    for event in selection.quarantined_events:
        if event.ticker is not None and event.document_type in TARGET_DOC_TYPE_CODES:
            events.setdefault(event.ticker, set()).add(event)
    tickers_by_doc = {
        doc_id: set(values) for doc_id, values in (retained_tickers_by_doc or {}).items()
    }
    if baseline is not None:
        # Retained receipts need not include a legacy baseline's original input.
        # Preserve that existing safety evidence even when L1 names another ticker.
        for ticker, row in baseline.rows.items():
            record = row.record
            if record.source_doc_id is not None and record.document_type in TARGET_DOC_TYPE_CODES:
                tickers_by_doc.setdefault(record.source_doc_id, set()).add(ticker)
    for event in selection.quarantined_events:
        if event.target_doc_id is not None:
            for ticker in tickers_by_doc.get(event.target_doc_id, ()):
                events.setdefault(ticker, set()).add(event)
    return MetricExtractionPlan(
        selection=selection,
        baseline=baseline,
        extractor_revision=extractor_revision,
        events_by_ticker={ticker: frozenset(items) for ticker, items in events.items()},
    )


def can_reuse_edinet_metric(
    *,
    candidate: EdinetDocumentCandidate,
    baseline_row: EDINETMetricBaselineRow,
    extractor_revision: str,
) -> bool:
    record = baseline_row.record
    return (
        DEFERRED_CSV_REASON not in record.failure_reasons
        and not any(
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
