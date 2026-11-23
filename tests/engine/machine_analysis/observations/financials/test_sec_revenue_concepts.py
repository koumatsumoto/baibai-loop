"""Issuer-wide tag coverage with a minimal, source-derived Apple filing fixture."""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import date, datetime
from decimal import Decimal
from hashlib import sha256

import pytest

from baibai_engine.machine_analysis.observations.financials import sec_companyfacts
from baibai_engine.machine_analysis.observations.financials.sec_companyfacts import (
    SecCompanyFactsCapture,
    SecFilingWitness,
    SecRevenueObservation,
    observe_sec_revenue,
)

_CIK = "0000320193"
_ORIGINAL = "0000320193-25-000073"
_COMPARATIVE = "0000320193-26-000020"
_LEGACY = "Revenues"
_CONTRACT = "RevenueFromContractWithCustomerExcludingAssessedTax"
_Q_START, _Q_END = date(2025, 3, 30), date(2025, 6, 28)
_YTD_START = date(2024, 9, 29)

# SEC filing 0000320193-25-000073 R2.htm and Apple CompanyConcept under _CONTRACT
# agree on these issuer-wide USD amounts. The compact JSON wraps only checked rows;
# originals and later comparative are different receipts. All reference IDs and
# exact clocks below are synthetic, including the publication witness.
_FILING = SecFilingWitness(
    issuer_cik=_CIK,
    accession=_ORIGINAL,
    form="10-Q",
    receipt_id="fixture:apple-original-filing",
    accepted_at=datetime.fromisoformat("2025-08-01T10:00:00Z"),
    published_at=datetime.fromisoformat("2025-08-01T10:05:00Z"),
    publication_ref="fixture:apple-original-publication",
    received_at=datetime.fromisoformat("2025-08-01T10:10:00Z"),
)
_RECEIVED = datetime.fromisoformat("2025-08-01T10:15:00Z")
_LATE_RECEIVED = datetime.fromisoformat("2026-08-01T10:15:00Z")


def _fact(
    start: date = _Q_START, value: object = 94_036_000_000, **overrides: object
) -> dict[str, object]:
    return {
        "start": start.isoformat(),
        "end": _Q_END.isoformat(),
        "val": value,
        "accn": _ORIGINAL,
        "form": "10-Q",
        **overrides,
    }


def _units(*facts: dict[str, object], unit: str = "USD") -> dict[str, object]:
    return {"units": {unit: facts}}


def _capture(
    concepts: dict[str, object],
    *,
    received: datetime = _RECEIVED,
    cik: int = 320193,
) -> SecCompanyFactsCapture:
    body = json.dumps({"cik": cik, "facts": {"us-gaap": concepts}}).encode()
    return SecCompanyFactsCapture(
        receipt_id=f"fixture:companyfacts:{received.isoformat()}",
        received_at=received,
        body_sha256=sha256(body).hexdigest(),
        body=body,
    )


def _observe(
    capture: SecCompanyFactsCapture,
    *,
    filing: SecFilingWitness = _FILING,
    start: date = _Q_START,
    cutoff: datetime = _RECEIVED,
):
    return observe_sec_revenue(
        capture,
        filing,
        expected_cik=_CIK,
        period_start=start,
        period_end=_Q_END,
        cutoff=cutoff,
    )


@pytest.mark.parametrize("reverse", [True, False])
def test_original_apple_consolidated_quarter_under_contract_tag_is_not_ytd(reverse: bool) -> None:
    quarter = _fact()
    nine_months = _fact(_YTD_START, 313_695_000_000)
    rows = (quarter, nine_months) if reverse else (nine_months, quarter)
    old_legacy = _fact(
        date(2018, 7, 1),
        62_900_000_000,
        end="2018-09-29",
        accn="0000320193-18-000145",
        form="10-K",
    )
    capture = _capture({_LEGACY: _units(old_legacy), _CONTRACT: _units(*rows)})
    result = _observe(capture)
    assert isinstance(result, SecRevenueObservation)
    assert result.amount == Decimal("94036000000")
    assert (result.period_start, result.period_end) == (_Q_START, _Q_END)
    assert (result.unit, result.scope, result.concept) == (
        "USD",
        "filing_entity",
        f"us-gaap:{_CONTRACT}",
    )
    assert result.fact_paths == (f"/facts/us-gaap/{_CONTRACT}/units/USD/{rows.index(quarter)}",)
    assert (result.filing.issuer_cik, result.filing.accession, result.filing.publication_ref) == (
        _CIK,
        _ORIGINAL,
        _FILING.publication_ref,
    )
    assert (result.companyfacts_receipt_id, result.companyfacts_body_sha256) == (
        capture.receipt_id,
        capture.body_sha256,
    )

    just_ytd = _capture({_CONTRACT: _units(nine_months)})
    assert _observe(just_ytd) == "no_fact_for_scope"
    ytd_result = _observe(just_ytd, start=_YTD_START)
    assert isinstance(ytd_result, SecRevenueObservation)
    assert ytd_result.amount == Decimal("313695000000")


def test_later_identical_comparative_can_neither_backfill_nor_replace_original() -> None:
    later = _fact(accn=_COMPARATIVE)
    later_only = _capture({_CONTRACT: _units(later)}, received=_LATE_RECEIVED)
    assert _observe(later_only) == "not_yet_received"
    assert _observe(later_only, cutoff=_LATE_RECEIVED) == "no_fact_for_scope"

    collected_late = _capture({_CONTRACT: _units(later, _fact())}, received=_LATE_RECEIVED)
    assert _observe(collected_late) == "not_yet_received"
    original = _observe(collected_late, cutoff=_LATE_RECEIVED)
    assert isinstance(original, SecRevenueObservation)
    assert original.filing.accession == _ORIGINAL
    assert original.fact_paths == (f"/facts/us-gaap/{_CONTRACT}/units/USD/1",)


@pytest.mark.parametrize("value", [94_036_000_000, 94_036_000_001])
@pytest.mark.parametrize("reverse", [True, False])
def test_competing_concepts_are_not_synonyms_even_when_their_values_match(
    value: int, reverse: bool
) -> None:
    pairs = [(_CONTRACT, _units(_fact())), (_LEGACY, _units(_fact(value=value)))]
    capture = _capture(dict(reversed(pairs)) if reverse else dict(pairs))
    result = _observe(capture)
    assert not isinstance(result, SecRevenueObservation)
    assert isinstance(result, sec_companyfacts.SecRevenueAmbiguity)
    expected_reason = (
        "conflicting_amounts" if value != 94_036_000_000 else "semantic_equivalence_unverified"
    )
    assert result.reason == expected_reason
    assert {
        (candidate.concept, candidate.amount, candidate.fact_paths)
        for candidate in result.candidates
    } == {
        (f"us-gaap:{_LEGACY}", Decimal(value), (f"/facts/us-gaap/{_LEGACY}/units/USD/0",)),
        (
            f"us-gaap:{_CONTRACT}",
            Decimal("94036000000"),
            (f"/facts/us-gaap/{_CONTRACT}/units/USD/0",),
        ),
    }


def test_new_concept_is_still_bound_to_issuer_usd_and_independent_publication() -> None:
    body = {_CONTRACT: _units(_fact())}
    source = _capture(body)
    no_publication = replace(_FILING, published_at=None, publication_ref=None)
    assert _observe(source, filing=no_publication) == "publication_unverified"
    with pytest.raises(ValueError, match="CompanyFacts CIK"):
        _observe(_capture(body, cik=320194))
    with pytest.raises(ValueError, match="received before witnessed filing publication"):
        _observe(_capture(body, received=datetime.fromisoformat("2025-08-01T10:04:59Z")))
    assert (
        _observe(_capture({_CONTRACT: _units(_fact(), unit="EUR")})) == "unmapped_concept_or_unit"
    )
    assert (
        _observe(
            _capture(
                {
                    "RevenueFromContractWithCustomerIncludingAssessedTax": _units(_fact()),
                    "SalesRevenueNet": _units(_fact()),
                }
            )
        )
        == "unmapped_concept_or_unit"
    )


@pytest.mark.parametrize(
    "bad", [_fact(value=False), _fact(form="10-K"), dict(_fact(), start="20250330")]
)
def test_malformed_current_alternate_cannot_hide_behind_a_valid_legacy_fact(bad) -> None:
    source = _capture({_LEGACY: _units(_fact()), _CONTRACT: _units(bad)})
    with pytest.raises(ValueError, match="SEC revenue"):
        _observe(source)


def test_conflicting_rows_inside_one_native_concept_remain_invalid() -> None:
    source = _capture({_CONTRACT: _units(_fact(), _fact(value=94_036_000_001))})
    with pytest.raises(ValueError, match="conflicting SEC revenue amounts"):
        _observe(source)


def test_competing_candidates_are_not_exposed_before_both_receipts_and_publication() -> None:
    source = _capture({_LEGACY: _units(_fact()), _CONTRACT: _units(_fact(value=1))})
    unpublished = replace(_FILING, published_at=None, publication_ref=None)
    assert _observe(source, filing=unpublished) == "publication_unverified"
    future = replace(_FILING, received_at=_LATE_RECEIVED)
    assert _observe(source, filing=future) == "not_yet_received"
