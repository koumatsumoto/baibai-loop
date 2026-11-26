from dataclasses import replace
from datetime import date, datetime, timedelta
from decimal import Decimal
from hashlib import sha256
from json import dumps

import pytest

from baibai_engine.machine_analysis.observations.financials.sec_companyfacts import (
    SecCompanyFactsCapture,
    SecFilingWitness,
    SecIssuerUsdObservation,
    SecReportedDuration,
    SecRevenueAmbiguity,
    observe_sec_comparatives,
)

_CIK = "0000000042"
_ACCESSION = "0000000042-26-000019"
_OTHER = "0000000042-26-000021"
_REVENUE = "Revenues"
_OTHER_REVENUE = "RevenueFromContractWithCustomerExcludingAssessedTax"
_INCOME = "OperatingIncomeLoss"
_CFO = "NetCashProvidedByUsedInOperatingActivities"
_PPE = "PaymentsToAcquirePropertyPlantAndEquipment"
_Q = SecReportedDuration(date(2026, 7, 1), date(2026, 9, 30))
_PRIOR_Q = SecReportedDuration(date(2025, 7, 1), date(2025, 9, 30))
_YTD = SecReportedDuration(date(2026, 1, 1), _Q.end)
_PRIOR_YTD = SecReportedDuration(date(2025, 1, 1), _PRIOR_Q.end)
_ACCEPTED = datetime.fromisoformat("2026-10-29T00:10:00Z")
_PUBLIC = _ACCEPTED + timedelta(seconds=20)
_RECEIPT = _PUBLIC + timedelta(days=1)
_FILING = SecFilingWitness(
    _CIK, _ACCESSION, "10-Q", "filing", _ACCEPTED, _PUBLIC, "public", _RECEIPT
)


def _fact(period, value, **overrides):
    return {
        "accn": _ACCESSION,
        "form": "10-Q",
        "start": period.start.isoformat(),
        "end": period.end.isoformat(),
        "val": value,
        "fy": 2026,
        "fp": "Q3",
        **overrides,
    }


def _sample():
    return {
        _REVENUE: [_fact(_YTD, 31), _fact(_Q, 12), _fact(_PRIOR_Q, 10)],
        _INCOME: [_fact(_YTD, 22), _fact(_PRIOR_Q, 7), _fact(_Q, 9)],
        _CFO: [_fact(_Q, 88), _fact(_YTD, -2), _fact(_PRIOR_YTD, 14)],
        _PPE: [_fact(_PRIOR_YTD, 3), _fact(_YTD, 15), _fact(_Q, 89)],
    }


def _capture(facts, *, cik=42):
    body = dumps(
        {
            "cik": cik,
            "facts": {
                "us-gaap": {
                    tag: {"units": amounts if isinstance(amounts, dict) else {"USD": amounts}}
                    for tag, amounts in facts.items()
                }
            },
        }
    ).encode()
    return SecCompanyFactsCapture("companyfacts", _RECEIPT, sha256(body).hexdigest(), body)


def _observe(source, **kwargs):
    return observe_sec_comparatives(
        source,
        _FILING,
        **{
            "expected_cik": _CIK,
            "reporting_quarter": "Q3",
            "current_quarter": _Q,
            "prior_quarter": _PRIOR_Q,
            "current_fiscal_start": _YTD.start,
            "prior_fiscal_start": _PRIOR_YTD.start,
            "cutoff": _RECEIPT,
            **kwargs,
        },
    )


def _by_metric(result):
    assert isinstance(result, tuple)
    return {row.metric: row for row in result}


@pytest.mark.parametrize("reverse", [False, True])
def test_exact_scope_and_sign_ignore_filing_fy_label_and_row_order(reverse):
    raw = _sample()
    if reverse:
        raw = {tag: rows[::-1] for tag, rows in raw.items()}
    capture = _capture(raw)
    result = _by_metric(_observe(capture))
    for metric, tag, periods, values, basis in (
        ("revenue", _REVENUE, (_Q, _PRIOR_Q), (12, 10), "quarter"),
        ("operating_income", _INCOME, (_Q, _PRIOR_Q), (9, 7), "quarter"),
        ("operating_cash_flow", _CFO, (_YTD, _PRIOR_YTD), (-2, 14), "year_to_date"),
        ("ppe_cash_paid", _PPE, (_YTD, _PRIOR_YTD), (15, 3), "year_to_date"),
    ):
        row = result[metric]
        assert (row.current_period, row.prior_period, row.basis) == (*periods, basis)
        for fact, period, value in zip((row.current, row.prior), periods, values, strict=True):
            assert isinstance(fact, SecIssuerUsdObservation)
            assert (fact.amount, fact.unit, fact.scope) == (Decimal(value), "USD", "filing_entity")
            assert fact.concept == f"us-gaap:{tag}"
            assert (fact.period_start, fact.period_end) == (period.start, period.end)
            assert (fact.filing, fact.companyfacts_body_sha256) == (_FILING, capture.body_sha256)
            assert fact.fact_paths == (
                f"/facts/us-gaap/{tag}/units/USD/{raw[tag].index(_fact(period, value))}",
            )


def test_cannot_fill_cash_with_other_filing_currency_or_quarter():
    raw = _sample()
    raw[_CFO] = [_fact(_Q, 77), _fact(_PRIOR_YTD, 11, accn=_OTHER)]
    raw[_PPE] = {"USD": [_fact(_PRIOR_YTD, 1)], "EUR": [_fact(_YTD, 14)]}
    rows = _by_metric(_observe(_capture(raw)))
    assert (
        rows["operating_cash_flow"].current
        == rows["operating_cash_flow"].prior
        == "no_fact_for_scope"
    )
    assert rows["ppe_cash_paid"].current == "no_fact_for_scope"
    assert isinstance(rows["ppe_cash_paid"].prior, SecIssuerUsdObservation)
    unmapped = _by_metric(_observe(_capture({_PPE: {"EUR": [_fact(_YTD, 4)]}})))
    assert unmapped["ppe_cash_paid"].current == "unmapped_concept_or_unit"


@pytest.mark.parametrize("bad", [False, 6])
def test_bad_or_conflicting_measure_does_not_hide_other_measure_or_period(bad):
    raw = _sample()
    raw[_PPE].append(_fact(_YTD, bad))
    raw[_CFO] = [_fact(_YTD, 0)]
    raw[_OTHER_REVENUE] = [_fact(_Q, 12)]
    rows = _by_metric(_observe(_capture(raw)))
    assert isinstance(rows["revenue"].current, SecRevenueAmbiguity)
    assert rows["revenue"].current.reason == "semantic_equivalence_unverified"
    assert rows["ppe_cash_paid"].current == "invalid_fact"
    assert isinstance(rows["ppe_cash_paid"].prior, SecIssuerUsdObservation)
    assert isinstance(rows["operating_cash_flow"].current, SecIssuerUsdObservation)
    assert rows["operating_cash_flow"].current.amount == 0


@pytest.mark.parametrize(
    ("kwargs", "reason"),
    [
        ({"current_quarter": _YTD}, "regular standalone"),
        ({"current_fiscal_start": date(2024, 1, 1)}, "regular Q1-Q3"),
        ({"prior_fiscal_start": date(2025, 4, 1)}, "selected reporting quarter"),
        ({"prior_fiscal_start": date(2024, 12, 21)}, "preceding fiscal year"),
        (
            {"current_fiscal_start": _Q.start, "prior_fiscal_start": _PRIOR_Q.start},
            "selected reporting quarter",
        ),
        ({"prior_quarter": _Q, "prior_fiscal_start": _YTD.start}, "preceding fiscal year"),
        (
            {
                "prior_quarter": SecReportedDuration(date(2024, 7, 1), date(2024, 9, 30)),
                "prior_fiscal_start": date(2024, 1, 1),
            },
            "preceding fiscal year",
        ),
    ],
)
def test_rejects_invalid_statement_pair(kwargs, reason):
    with pytest.raises(ValueError, match=reason):
        _observe(_capture(_sample()), **kwargs)


@pytest.mark.parametrize("days", [364, 371])
def test_52_and_53_week_comparison_columns_are_allowed(days):
    shift = timedelta(days=days)
    prior_quarter = SecReportedDuration(_Q.start - shift, _Q.end - shift)
    result = _observe(
        _capture(_sample()), prior_quarter=prior_quarter, prior_fiscal_start=_YTD.start - shift
    )
    assert isinstance(result, tuple)


def test_q1_can_share_dates_without_merging_statement_bases():
    current = SecReportedDuration(date(2026, 1, 1), date(2026, 3, 31))
    prior = SecReportedDuration(date(2025, 1, 1), date(2025, 3, 31))
    rows = _by_metric(
        _observe(
            _capture({_REVENUE: [_fact(current, 54)], _CFO: [_fact(current, 31)]}),
            reporting_quarter="Q1",
            current_quarter=current,
            prior_quarter=prior,
            current_fiscal_start=current.start,
            prior_fiscal_start=prior.start,
        )
    )
    assert isinstance(rows["revenue"].current, SecIssuerUsdObservation)
    assert isinstance(rows["operating_cash_flow"].current, SecIssuerUsdObservation)
    assert rows["revenue"].basis != rows["operating_cash_flow"].basis


def test_fresh_comparison_does_not_bypass_bound_receipt_or_issuer():
    source = _capture(_sample())
    assert _observe(source, cutoff=_RECEIPT - timedelta(seconds=1)) == "not_yet_received"
    with pytest.raises(ValueError, match="CompanyFacts CIK"):
        _observe(_capture(_sample(), cik=27))
    with pytest.raises(ValueError, match="SHA-256"):
        _observe(replace(source, body=source.body + b" "))
