"""The extraction plan selects only source archives needed for parsing."""

import json
from datetime import UTC, date, datetime, timedelta

import pytest
from tests.helpers.edinet_metrics import _edinet_document

from baibai_engine.data_collection.providers.edinet.receipts import retain_list_response
from baibai_engine.machine_analysis.observations.disclosures.edinet_document_events import (
    QuarantinedDocumentEvent,
)
from baibai_engine.machine_analysis.observations.disclosures.edinet_selection import (
    DocumentSelection,
    EdinetDocumentCandidate,
)
from baibai_engine.machine_analysis.observations.financials.edinet_metrics.planning import (
    make_metric_extraction_plan,
    read_retained_metric_document_tickers,
)
from baibai_engine.machine_analysis.observations.financials.edinet_metrics.store import (
    EDINETMetricBaseline,
    EDINETMetricBaselineRow,
)
from baibai_engine.machine_analysis.observations.financials.edinet_models import EdinetMetricRecord
from baibai_engine.storage.sqlite.source_cache.schema import open_connection


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


def _withdrawal_selection() -> DocumentSelection:
    return DocumentSelection(
        candidates={},
        quarantined_events=(
            QuarantinedDocumentEvent(
                doc_id="S100WITHDRAW", target_doc_id="S100ORIGIN", event_type="withdrawal"
            ),
        ),
    )


def _retain(connection, at, documents, *, day=date(2026, 4, 24), count=None):
    return retain_list_response(
        connection,
        doc_date=day,
        received_at=at,
        body=json.dumps(
            {
                "metadata": {"resultset": {"count": len(documents) if count is None else count}},
                "results": [{**item, "seqNumber": n} for n, item in enumerate(documents, 1)],
            }
        ).encode(),
    )


def _origin(ticker="72030", doc_type="120"):
    return _edinet_document(ticker=ticker, doc_id="S100ORIGIN", doc_type=doc_type)


def test_original_identity_survives_omission_without_requiring_l2_or_losing_legacy_evidence(
    tmp_path,
):
    path = tmp_path / "market.sqlite"
    connection = open_connection(path)
    at = datetime(2026, 4, 24, 12, tzinfo=UTC)
    _retain(connection, at, [_origin()])
    _retain(connection, at + timedelta(seconds=1), [])
    connection.close()
    before = path.read_bytes()
    selection = _withdrawal_selection()
    retained = read_retained_metric_document_tickers(
        path, selection=selection, through=date(2026, 4, 27)
    )
    assert retained == {"S100ORIGIN": frozenset({"7203"})}
    assert path.read_bytes() == before
    legacy = EdinetDocumentCandidate(ticker="8306", doc_id="S100ORIGIN", doc_type_code="120")
    for baseline in (
        None,
        EDINETMetricBaseline(asof_date=date(2026, 4, 24), rows={"8306": _baseline_row(legacy)}),
    ):
        plan = make_metric_extraction_plan(
            selection=selection,
            baseline=baseline,
            extractor_revision="metric-v2",
            retained_tickers_by_doc=retained,
        )
        tickers = {"7203"} if baseline is None else {"7203", "8306"}
        assert plan.events_by_ticker == {
            ticker: frozenset(selection.quarantined_events) for ticker in tickers
        }
        assert plan.selection is selection  # Historical identity is not a present filing.
        assert plan.archive_candidates == ()


@pytest.mark.parametrize("unusable", ["invalid_day", "nonmetric", "invalid_ticker", "future_day"])
def test_unusable_or_future_original_does_not_invent_target_identity(tmp_path, unusable):
    path = tmp_path / "market.sqlite"
    connection = open_connection(path)
    _retain(
        connection,
        datetime(2026, 4, 24, 12, tzinfo=UTC),
        [
            _origin(
                ticker="bad" if unusable == "invalid_ticker" else "72030",
                doc_type="350" if unusable == "nonmetric" else "120",
            )
        ],
        day=date(2026, 4, 28) if unusable == "future_day" else date(2026, 4, 24),
        count=2 if unusable == "invalid_day" else None,
    )
    connection.close()
    assert (
        read_retained_metric_document_tickers(
            path, selection=_withdrawal_selection(), through=date(2026, 4, 27)
        )
        == {}
    )


def test_ambiguous_receipt_order_cannot_supply_conflicting_or_later_identity(tmp_path):
    path = tmp_path / "market.sqlite"
    connection = open_connection(path)
    at = datetime(2026, 4, 24, 12, tzinfo=UTC)
    _retain(connection, at, [_origin()])
    for ticker in ("83060", "67580"):
        _retain(connection, at + timedelta(seconds=1), [_origin(ticker)])
    _retain(connection, at + timedelta(seconds=2), [_origin("96820")])
    connection.close()
    assert read_retained_metric_document_tickers(
        path, selection=_withdrawal_selection(), through=date(2026, 4, 27)
    ) == {"S100ORIGIN": frozenset({"7203"})}


def test_invalid_receipt_does_not_erase_previously_observed_identity(tmp_path):
    path = tmp_path / "market.sqlite"
    connection = open_connection(path)
    at = datetime(2026, 4, 24, 12, tzinfo=UTC)
    _retain(connection, at, [_origin()])
    _retain(connection, at + timedelta(seconds=1), [_origin("83060")], count=2)
    connection.close()
    assert read_retained_metric_document_tickers(
        path, selection=_withdrawal_selection(), through=date(2026, 4, 27)
    ) == {"S100ORIGIN": frozenset({"7203"})}


def test_no_event_does_not_open_receipt_history(tmp_path, monkeypatch):
    from baibai_engine.machine_analysis.observations.financials.edinet_metrics import planning

    path = tmp_path / "unrelated.sqlite"
    path.write_bytes(b"not a database")
    monkeypatch.setattr(planning, "connect_read_only", lambda *_: pytest.fail("opened history"))
    assert (
        read_retained_metric_document_tickers(
            path, selection=DocumentSelection(candidates={}), through=date(2026, 4, 27)
        )
        == {}
    )


def test_null_or_nonmetric_metadata_never_becomes_an_issuer_ticker_join(tmp_path):
    path = tmp_path / "market.sqlite"
    connection = open_connection(path)
    at = datetime(2026, 4, 24, 12, tzinfo=UTC)
    _retain(connection, at, [_origin()])
    _retain(
        connection,
        at + timedelta(seconds=1),
        [{**_origin(), "secCode": None, "docTypeCode": None, "edinetCode": "E83060"}],
    )
    _retain(
        connection,
        at + timedelta(seconds=2),
        [{**_origin("67580", "350"), "issuerEdinetCode": "E72030"}],
    )
    connection.close()
    assert read_retained_metric_document_tickers(
        path, selection=_withdrawal_selection(), through=date(2026, 4, 27)
    ) == {"S100ORIGIN": frozenset({"7203"})}


def test_multiple_observed_tickers_are_all_quarantined_without_uuid_or_baseline_choice(tmp_path):
    path = tmp_path / "market.sqlite"
    connection = open_connection(path)
    at = datetime(2026, 4, 24, 12, tzinfo=UTC)
    _retain(connection, at, [_origin()])
    _retain(connection, at, [_origin()])  # Same bytes/time are not an ambiguous source order.
    _retain(connection, at + timedelta(seconds=1), [_origin("83060")])
    connection.close()
    selection = _withdrawal_selection()
    retained = read_retained_metric_document_tickers(
        path, selection=selection, through=date(2026, 4, 27)
    )
    assert retained == {"S100ORIGIN": frozenset({"7203", "8306"})}
    plan = make_metric_extraction_plan(
        selection=selection,
        baseline=None,
        extractor_revision="metric-v2",
        retained_tickers_by_doc=retained,
    )
    assert set(plan.events_by_ticker) == {"7203", "8306"}


def test_absent_original_identity_is_a_real_limit_not_a_reconstructible_metric(tmp_path):
    path = tmp_path / "market.sqlite"
    connection = open_connection(path)
    _retain(connection, datetime(2026, 4, 24, 12, tzinfo=UTC), [])
    connection.close()
    selection = _withdrawal_selection()
    retained = read_retained_metric_document_tickers(
        path, selection=selection, through=date(2026, 4, 27)
    )
    assert retained == {}
    cold = make_metric_extraction_plan(
        selection=selection,
        baseline=None,
        extractor_revision="metric-v2",
        retained_tickers_by_doc=retained,
    )
    assert cold.events_by_ticker == {}
    # The exact same raw history permits different legacy-only identities.
    # Neither can be inferred from the anonymous target docID.
    for ticker in ("7203", "8306"):
        original = EdinetDocumentCandidate(ticker=ticker, doc_id="S100ORIGIN", doc_type_code="120")
        warm = make_metric_extraction_plan(
            selection=selection,
            baseline=EDINETMetricBaseline(
                asof_date=date(2026, 4, 24), rows={ticker: _baseline_row(original)}
            ),
            extractor_revision="metric-v2",
            retained_tickers_by_doc=retained,
        )
        assert set(warm.events_by_ticker) == {ticker}
