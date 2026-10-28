"""The extraction plan selects only source archives needed for parsing."""

from datetime import date

from baibai_engine.machine_analysis.observations.disclosures.edinet_document_events import QuarantinedDocumentEvent
from baibai_engine.market.edinet_metrics.planning import make_metric_extraction_plan
from baibai_engine.market.edinet_metrics.store import EDINETMetricBaseline, EDINETMetricBaselineRow
from baibai_engine.machine_analysis.observations.financials.edinet_models import EdinetMetricRecord
from baibai_engine.machine_analysis.observations.disclosures.edinet_selection import DocumentSelection, EdinetDocumentCandidate


def _candidate(ticker: str) -> EdinetDocumentCandidate:
    return EdinetDocumentCandidate(
        ticker=ticker,
        doc_id=f"S100{ticker}",
        doc_type_code="120",
        source_document_revision="document-v2",
    )


def _baseline_row(
    candidate: EdinetDocumentCandidate,
    *,
    extractor_revision: str = "metric-v2",
    source_document_revision: str = "document-v2",
) -> EDINETMetricBaselineRow:
    return EDINETMetricBaselineRow(
        record=EdinetMetricRecord(
            ticker=candidate.ticker,
            source_doc_id=candidate.doc_id,
            document_type=candidate.doc_type_code,
        ),
        extractor_revision=extractor_revision,
        source_document_revision=source_document_revision,
    )


def test_archive_candidates_exclude_quarantined_and_reusable_filings() -> None:
    candidates = {
        ticker: _candidate(ticker) for ticker in ("9682", "8306", "1301", "8058", "7203", "6758")
    }
    direct_event = QuarantinedDocumentEvent(
        doc_id="S100DIRECT",
        target_doc_id=None,
        event_type="edit",
        document_type="120",
        ticker="1301",
    )
    target_event = QuarantinedDocumentEvent(
        doc_id="S100WITHDRAW",
        target_doc_id=candidates["7203"].doc_id,
        event_type="withdrawal",
    )
    baseline = EDINETMetricBaseline(
        asof_date=date(2026, 4, 24),
        rows={
            "7203": _baseline_row(candidates["7203"]),
            "9682": _baseline_row(candidates["9682"]),
            "6758": _baseline_row(candidates["6758"], extractor_revision="metric-v1"),
            "8058": _baseline_row(candidates["8058"], source_document_revision="document-v1"),
        },
    )

    plan = make_metric_extraction_plan(
        selection=DocumentSelection(
            candidates=candidates, quarantined_events=(direct_event, target_event)
        ),
        baseline=baseline,
        extractor_revision="metric-v2",
    )

    assert plan.events_by_ticker == {
        "1301": frozenset({direct_event}),
        "7203": frozenset({target_event}),
    }
    assert tuple(candidate.ticker for candidate in plan.archive_candidates) == (
        "6758",  # Extractor revision changed.
        "8058",  # Source document revision changed.
        "8306",  # No baseline row exists.
    )


def test_archive_candidates_without_baseline_include_unquarantined_filings() -> None:
    candidates = {ticker: _candidate(ticker) for ticker in ("9682", "7203")}
    event = QuarantinedDocumentEvent(
        doc_id="S100EDIT",
        target_doc_id=None,
        event_type="edit",
        document_type="120",
        ticker="9682",
    )
    plan = make_metric_extraction_plan(
        selection=DocumentSelection(candidates=candidates, quarantined_events=(event,)),
        baseline=None,
        extractor_revision="metric-v2",
    )

    assert tuple(candidate.ticker for candidate in plan.archive_candidates) == ("7203",)
