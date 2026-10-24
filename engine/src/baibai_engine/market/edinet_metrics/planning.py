"""Determine extraction inputs from one document selection and metric baseline."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from baibai_engine.market.edinet_document_events import QuarantinedDocumentEvent
from baibai_engine.market.edinet_metrics.revision import DEFERRED_CSV_REASON
from baibai_engine.market.edinet_metrics.store import EDINETMetricBaseline, EDINETMetricBaselineRow
from baibai_engine.market.edinet_selection import (
    TARGET_DOC_TYPE_CODES,
    DocumentSelection,
    EdinetDocumentCandidate,
)


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


def make_metric_extraction_plan(
    *,
    selection: DocumentSelection,
    baseline: EDINETMetricBaseline | None,
    extractor_revision: str,
) -> MetricExtractionPlan:
    events: dict[str, set[QuarantinedDocumentEvent]] = {}
    for event in selection.quarantined_events:
        if event.ticker is not None and event.document_type in TARGET_DOC_TYPE_CODES:
            events.setdefault(event.ticker, set()).add(event)
    if baseline is not None:
        tickers_by_doc: dict[str, set[str]] = {}
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
