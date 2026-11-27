"""This suite uses shaped fixtures, not live SEC responses."""

from dataclasses import replace
from datetime import date, datetime
from decimal import Decimal
from hashlib import sha256
from json import dumps
from uuid import UUID
from zoneinfo import ZoneInfo

import pytest
from httpx2 import ByteStream, MockTransport, Response

from baibai_engine.data_collection.providers.sec_edgar import (
    SecEdgarQuery,
    SecReportedTicker,
    capture_sec_edgar,
    observe_reported_ticker,
)
from baibai_engine.machine_analysis.observations.financials import sec_fiscal_observations
from baibai_engine.machine_analysis.observations.financials.sec_companyfacts import (
    SecCompanyFactsCapture,
    SecFilingWitness,
    SecIssuerUsdObservation,
    SecReportedDuration,
)
from baibai_engine.machine_analysis.observations.financials.sec_fiscal_observations import (
    SecFiscalQuarterInput,
    observe_sec_issuer_financials,
)

_NVDA = "0001045810"
_ALPHABET = "0001652044"
_CUTOFF = datetime.fromisoformat("2026-11-27T22:14:00Z")
_ACCEPTED = datetime.fromisoformat("2026-08-26T20:36:00Z")
_CURRENT = SecReportedDuration(date(2026, 4, 27), date(2026, 7, 26))
_PRIOR = SecReportedDuration(date(2025, 4, 28), date(2025, 7, 27))
_CURRENT_START = date(2026, 1, 26)
_PRIOR_START = date(2025, 1, 27)
_Q3_END = date(2026, 10, 25)
_REVENUE = "Revenues"
_CFO = "NetCashProvidedByUsedInOperatingActivities"


def _subjects(cik, *symbols):
    query = SecEdgarQuery("submissions", cik)
    body = dumps(
        {
            "cik": int(cik),
            "tickers": symbols,
            "filings": {
                "recent": {"accessionNumber": [], "form": [], "acceptanceDateTime": []},
                "files": [],
            },
        }
    ).encode()
    receipt = capture_sec_edgar(
        query,
        transport=MockTransport(lambda req: Response(200, request=req, stream=ByteStream(body))),
        contact_email="team@baibai.test",
        attempt_id=UUID(int=1),
        clock=lambda: _CUTOFF,
    )
    assert not isinstance(receipt, str)
    assert receipt.state == "received"
    result = tuple(
        observe_reported_ticker(receipt, symbol=symbol, expected_cik=cik, cutoff=_CUTOFF)
        for symbol in symbols
    )
    assert all(isinstance(subject, SecReportedTicker) for subject in result)
    return result


def _quarter(cik, *, conflicting_revenues=False):
    accession = f"{cik}-26-000075"
    witness = SecFilingWitness(
        cik, accession, "10-Q", "filing", _ACCEPTED, _ACCEPTED, "publication", _CUTOFF
    )

    def fact(start, end, amount):
        return {
            "accn": accession,
            "form": "10-Q",
            "start": start.isoformat(),
            "end": end.isoformat(),
            "val": amount,
        }

    revenue = [
        fact(_CURRENT.start, _CURRENT.end, 96_221_000_000),
        fact(_PRIOR.start, _PRIOR.end, 10),
    ]
    if conflicting_revenues:
        revenue.append(fact(_CURRENT.start, _CURRENT.end, 9))
    # A later earnings exhibit cannot advance the periodic filing's context.
    revenue.append(
        {
            **fact(date(2026, 7, 27), _Q3_END, 115_844_000_000),
            "accn": f"{cik}-26-000088",
            "form": "8-K",
        }
    )
    cash = [
        fact(_CURRENT_START, _CURRENT.end, 74),
        fact(_PRIOR_START, _PRIOR.end, 21),
        fact(_CURRENT.start, _CURRENT.end, 30),
    ]
    body = dumps(
        {
            "cik": int(cik),
            "facts": {
                "us-gaap": {_REVENUE: {"units": {"USD": revenue}}, _CFO: {"units": {"USD": cash}}}
            },
        }
    ).encode()
    capture = SecCompanyFactsCapture("facts", _CUTOFF, sha256(body).hexdigest(), body)
    return SecFiscalQuarterInput(
        capture, witness, "Q2", _CURRENT, _PRIOR, _CURRENT_START, _PRIOR_START
    )


def _metric(issuer, metric):
    assert isinstance(issuer.periodic, tuple)
    return next(row for row in issuer.periodic if row.metric == metric)


def test_join_reads_one_issuer_once_without_collapsing_alphabet_stock_classes(monkeypatch):
    subjects = _subjects(_ALPHABET, "GOOGL", "GOOG")
    calls = []
    original = sec_fiscal_observations.observe_sec_comparatives

    def observe(*args, **kwargs):
        calls.append(kwargs["expected_cik"])
        return original(*args, **kwargs)

    monkeypatch.setattr(sec_fiscal_observations, "observe_sec_comparatives", observe)
    quarter = _quarter(_ALPHABET)
    (issuer,) = observe_sec_issuer_financials(subjects, {_ALPHABET: quarter}, cutoff=_CUTOFF)

    assert calls == [_ALPHABET]
    assert issuer.symbols == ("GOOG", "GOOGL")
    assert issuer.issuer_cik == _ALPHABET
    assert _metric(issuer, "revenue").current.scope == "filing_entity"


def test_later_8k_does_not_advance_selected_nvda_quarter_or_become_periodic_filing():
    selected = _quarter(_NVDA)
    subjects = _subjects(_NVDA, "NVDA")
    (issuer,) = observe_sec_issuer_financials(subjects, {_NVDA: selected}, cutoff=_CUTOFF)

    assert issuer.periodic_period_end == _CURRENT.end
    assert issuer.periodic_accession == "0001045810-26-000075"
    revenue = _metric(issuer, "revenue").current
    assert isinstance(revenue, SecIssuerUsdObservation)
    assert (revenue.amount, revenue.filing.accession) == (
        Decimal(96_221_000_000),
        "0001045810-26-000075",
    )

    unsupported = replace(
        selected,
        filing=replace(selected.filing, accession="0001045810-26-000088", form="8-K"),
        reporting_quarter="Q3",
        current_quarter=SecReportedDuration(date(2026, 7, 27), _Q3_END),
        prior_quarter=SecReportedDuration(date(2025, 7, 28), date(2025, 10, 26)),
    )
    (rejected,) = observe_sec_issuer_financials(subjects, {_NVDA: unsupported}, cutoff=_CUTOFF)
    assert rejected.periodic == "invalid_periodic_evidence"
    assert rejected.periodic_period_end is None
    assert rejected.periodic_accession is None


def test_comparison_conflict_is_local_and_cash_flow_matches_ytd_not_q2():
    subjects = (*_subjects(_NVDA, "NVDA"), *_subjects(_ALPHABET, "GOOG", "GOOGL"))
    nvda, alphabet = observe_sec_issuer_financials(
        subjects, {_NVDA: _quarter(_NVDA, conflicting_revenues=True)}, cutoff=_CUTOFF
    )
    sales = _metric(nvda, "revenue")
    assert sales.basis == "quarter"
    assert sales.current == "invalid_fact"
    assert sales.prior.amount == Decimal(10)
    cash = _metric(nvda, "operating_cash_flow")
    assert cash.basis == "year_to_date"
    assert (cash.current_period.start, cash.prior_period.start) == (_CURRENT_START, _PRIOR_START)
    assert (cash.current.amount, cash.prior.amount) == (Decimal(74), Decimal(21))
    assert alphabet.symbols == ("GOOG", "GOOGL")
    assert alphabet.periodic == "periodic_source_missing"


def test_other_issuer_packet_cannot_bind_to_verified_stock_labels():
    subjects = (*_subjects(_NVDA, "NVDA"), *_subjects(_ALPHABET, "GOOG", "GOOGL"))
    foreign = _quarter(_NVDA)
    nvda, alphabet = observe_sec_issuer_financials(
        subjects, {_NVDA: foreign, _ALPHABET: foreign}, cutoff=_CUTOFF
    )
    assert isinstance(_metric(nvda, "revenue").current, SecIssuerUsdObservation)
    assert alphabet.symbols == ("GOOG", "GOOGL")
    assert alphabet.periodic == "invalid_periodic_evidence"


def test_cik_aliases_cannot_choose_different_receipts_based_on_mapping_order():
    ny = ZoneInfo("America/New_York")
    early = datetime(2026, 11, 1, 1, 15, tzinfo=ny)
    later = early.replace(fold=1)
    cutoff = early.replace(minute=30)
    selected = _quarter(_NVDA)
    early_quarter = replace(selected, capture=replace(selected.capture, received_at=early))
    later_quarter = replace(selected, capture=replace(selected.capture, received_at=later))

    for keys in (
        {_NVDA: early_quarter, "1045810": later_quarter},
        {"1045810": later_quarter, _NVDA: early_quarter},
    ):
        with pytest.raises(ValueError, match="canonical CIK"):
            observe_sec_issuer_financials((), keys, cutoff=cutoff)

    (issuer,) = observe_sec_issuer_financials((), {_NVDA: later_quarter}, cutoff=cutoff)
    assert issuer.periodic == "not_yet_received"
    assert issuer.symbols == ()
