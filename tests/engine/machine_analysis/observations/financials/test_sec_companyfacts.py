from __future__ import annotations

import json
from dataclasses import replace
from datetime import date, datetime
from decimal import Decimal
from hashlib import sha256

import pytest

from baibai_engine.machine_analysis.observations.financials.sec_companyfacts import (
    SecCompanyFactsCapture,
    SecFilingWitness,
    SecPublicFilingObservation,
    SecRevenueObservation,
    observe_sec_revenue,
)

_CIK = "0000123456"
_ACCESSION = "0000999999-26-000001"  # The accession prefix is not the issuer CIK.
_AMENDMENT = "0000999999-26-000002"
_Q_START, _Q_END = date(2026, 7, 1), date(2026, 9, 30)


def _instant(s: str) -> datetime:
    return datetime.fromisoformat(s)


_CUTOFF = _instant("2026-11-25T21:12:00Z")
_FILING = SecFilingWitness(
    issuer_cik=_CIK,
    accession=_ACCESSION,
    form="10-Q",
    receipt_id="filing-receipt-a",
    accepted_at=_instant("2026-11-25T20:55:00Z"),
    published_at=_instant("2026-11-25T21:00:00Z"),
    publication_ref="public-witness-a",
    received_at=_instant("2026-11-25T21:05:00Z"),
)


def _fact(**fields: object) -> dict[str, object]:
    return {
        "accn": _ACCESSION,
        "form": "10-Q",
        "start": "2026-07-01",
        "end": "2026-09-30",
        "val": 5000,
        **fields,
    }


def _body(*rows: dict[str, object], cik: object = 123456, unit: str = "USD") -> bytes:
    return json.dumps(
        {"cik": cik, "facts": {"us-gaap": {"Revenues": {"units": {unit: rows}}}}}
    ).encode()


def _capture(body: bytes, *, received: str = "2026-11-25T21:07:00Z") -> SecCompanyFactsCapture:
    return SecCompanyFactsCapture(
        receipt_id="companyfacts-capture-a",
        body=body,
        body_sha256=sha256(body).hexdigest(),
        received_at=_instant(received),
    )


def _read(
    capture: SecCompanyFactsCapture,
    *,
    filing: SecFilingWitness = _FILING,
    cutoff: datetime = _CUTOFF,
    expected_cik: str = _CIK,
):
    return observe_sec_revenue(
        capture,
        filing,
        expected_cik=expected_cik,
        period_start=_Q_START,
        period_end=_Q_END,
        cutoff=cutoff,
    )


@pytest.mark.parametrize("reverse", [False, True])
def test_same_end_ytd_never_replaces_exact_quarter(reverse: bool) -> None:
    quarter, ytd = _fact(), _fact(start="2026-01-01", val=9000)
    rows = [ytd, quarter] if reverse else [quarter, ytd]
    source = _capture(_body(*rows))
    result = _read(source)
    assert isinstance(result, SecRevenueObservation)
    assert result.amount == Decimal(5000)
    assert (result.concept, result.unit, result.scope) == (
        "us-gaap:Revenues",
        "USD",
        "filing_entity",
    )
    assert (result.period_start, result.period_end) == (_Q_START, _Q_END)
    assert result.filing == _FILING
    assert (result.companyfacts_receipt_id, result.companyfacts_body_sha256) == (
        source.receipt_id,
        source.body_sha256,
    )
    assert result.fact_paths == (f"/facts/us-gaap/Revenues/units/USD/{rows.index(quarter)}",)


def test_keeps_raw_decimal_precision_and_zero_is_observed() -> None:
    spelling = "123456789012345678901.012345678901"
    raw = _body(_fact(val="DECIMAL_MARKER")).replace(b'"DECIMAL_MARKER"', spelling.encode())
    result = _read(_capture(raw))
    assert isinstance(result, SecRevenueObservation)
    assert str(result.amount) == spelling

    result = _read(_capture(_body(_fact(val=0))))
    assert isinstance(result, SecRevenueObservation)
    assert result.amount == Decimal(0)


def test_same_value_in_two_exact_rows_keeps_both_origin_paths() -> None:
    result = _read(_capture(_body(_fact(val=5), _fact(val=5.0))))
    assert isinstance(result, SecRevenueObservation)
    assert result.amount == Decimal(5)
    assert result.fact_paths == (
        "/facts/us-gaap/Revenues/units/USD/0",
        "/facts/us-gaap/Revenues/units/USD/1",
    )


@pytest.mark.parametrize("amounts", [(1, 2), (2, 1)])
def test_conflicting_amounts_in_same_accession_and_period_are_not_missing(amounts) -> None:
    with pytest.raises(ValueError, match="conflicting SEC revenue amounts"):
        _read(_capture(_body(*(_fact(val=amount) for amount in amounts))))


@pytest.mark.parametrize("amount", [False, True, None, "5000"])
def test_non_numbers_cannot_borrow_the_equal_valid_row(amount: object) -> None:
    for rows in ((_fact(val=1), _fact(val=amount)), (_fact(val=amount), _fact(val=1))):
        with pytest.raises(ValueError, match="SEC revenue value must be a JSON number"):
            _read(_capture(_body(*rows)))


def test_absence_in_valid_scope_is_different_from_unmapped_tag_or_broken_structure() -> None:
    assert _read(_capture(_body())) == "no_fact_for_scope"
    assert _read(_capture(_body(_fact(start="2026-01-01")))) == "no_fact_for_scope"
    assert _read(_capture(_body(_fact(), unit="EUR"))) == "unmapped_concept_or_unit"
    assert _read(_capture(b'{"cik":123456,"facts":{"us-gaap":{}}}')) == "unmapped_concept_or_unit"

    for malformed in (
        b'{"cik":123456}',
        b'{"cik":123456,"facts":{"us-gaap":{"Revenues":{}}}}',
        b'{"cik":123456,"facts":{"us-gaap":{"Revenues":{"units":{"USD":0}}}}}',
        b'{"cik":123456,"facts":{"us-gaap":{"Revenues":{"units":{"USD":null}}}}}',
    ):
        with pytest.raises(ValueError, match="invalid SEC CompanyFacts"):
            _read(_capture(malformed))


def test_exact_raw_hash_and_unambiguous_json_are_required() -> None:
    capture = _capture(_body(_fact()))
    with pytest.raises(ValueError, match="CompanyFacts body does not match its receipt SHA-256"):
        _read(replace(capture, body=capture.body + b"\n"))
    for raw in (
        b'{"cik":123456',
        _body(_fact()).replace(b'"val": 5000', b'"val": NaN'),
        _body(_fact()).replace(b'"val": 5000', b'"val": 1, "val": 2'),
    ):
        with pytest.raises(ValueError, match="invalid SEC CompanyFacts JSON"):
            _read(_capture(raw))


def test_expected_issuer_binds_companyfacts_and_filing_separately() -> None:
    good = _capture(_body(_fact(), cik=_CIK))
    assert isinstance(_read(good, expected_cik="123456"), SecRevenueObservation)
    with pytest.raises(ValueError, match="filing witness CIK does not match the expected issuer"):
        _read(good, filing=replace(_FILING, issuer_cik="0000999999"))
    with pytest.raises(ValueError, match="CompanyFacts CIK does not match the expected issuer"):
        _read(_capture(_body(_fact(), cik=999999)))
    for cik in (True, 0, "0000000000", "１23456"):
        with pytest.raises(ValueError, match="invalid SEC CIK"):
            _read(_capture(_body(_fact(), cik=cik)))


def test_typed_output_cik_is_canonical_without_changing_its_receipt_evidence() -> None:
    short_witness = replace(_FILING, issuer_cik="123456")
    original = _capture(_body(_fact(), cik=123456))
    result = _read(original, filing=short_witness)
    assert isinstance(result, SecRevenueObservation)
    assert result.filing == _FILING
    assert short_witness.issuer_cik == "123456"


def test_numeric_json_cik_does_not_authorize_numerical_typed_witness_cik() -> None:
    original = _capture(_body(_fact(), cik=123456))
    bad_witness = replace(_FILING, issuer_cik=123456)
    with pytest.raises(ValueError, match="invalid SEC CIK"):
        _read(original, filing=bad_witness)


def test_acceptance_publication_and_both_receipts_are_separate() -> None:
    source = _capture(_body(_fact()))
    assert _read(source, cutoff=_instant("2026-11-25T21:06:59Z")) == "not_yet_received"
    assert isinstance(_read(source, cutoff=source.received_at), SecRevenueObservation)
    assert (
        _read(source, filing=replace(_FILING, published_at=None, publication_ref=None))
        == "publication_unverified"
    )
    with pytest.raises(ValueError, match="publication time and witness must be supplied together"):
        _read(source, filing=replace(_FILING, publication_ref=None))

    earlier = _capture(source.body, received="2026-11-25T21:03:00Z")
    assert _read(earlier, cutoff=_instant("2026-11-25T21:04:00Z")) == "not_yet_received"
    assert isinstance(_read(earlier, cutoff=_FILING.received_at), SecRevenueObservation)

    with pytest.raises(
        ValueError, match="CompanyFacts fact was received before witnessed filing publication"
    ):
        _read(_capture(source.body, received="2026-11-25T20:59:00Z"))


def test_exact_accession_only_amendment_never_implicitly_replaces_original() -> None:
    raw = _body(_fact(accn=_AMENDMENT, form="10-Q/A", val=4200), _fact())
    source = _capture(raw)
    original = _read(source)
    assert isinstance(original, SecRevenueObservation)
    assert original.amount == Decimal(5000)

    corrected_witness = replace(
        _FILING, accession=_AMENDMENT, form="10-Q/A", receipt_id="filing-receipt-b"
    )
    corrected = _read(source, filing=corrected_witness)
    assert isinstance(corrected, SecRevenueObservation)
    assert corrected.amount == Decimal(4200)
    assert corrected.filing.accession == _AMENDMENT

    older_source = _capture(_body(_fact()), received="2026-11-25T20:57:00Z")
    assert _read(older_source, filing=corrected_witness) == "no_fact_for_scope"


@pytest.mark.parametrize("bad_start", [None, "20260701", "2026-11-30"])
def test_malformed_period_is_not_a_different_reporting_duration(bad_start: object) -> None:
    with pytest.raises(ValueError, match="invalid SEC revenue period"):
        _read(_capture(_body(_fact(start=bad_start))))


def test_mismatched_form_does_not_become_unavailable() -> None:
    with pytest.raises(ValueError, match="SEC revenue fact form does not match the filing"):
        _read(_capture(_body(_fact(form="10-K"))))


@pytest.mark.parametrize("include_quarter", [False, True])
def test_same_accession_form_mismatch_is_not_hidden_by_duration(include_quarter: bool) -> None:
    wrong_form_ytd = _fact(start="2026-01-01", form="10-K")
    rows = (wrong_form_ytd, _fact()) if include_quarter else (wrong_form_ytd,)
    with pytest.raises(ValueError, match="SEC revenue fact form does not match the filing"):
        _read(_capture(_body(*rows)))

    unrelated = {**wrong_form_ytd, "accn": _AMENDMENT}
    assert isinstance(_read(_capture(_body(unrelated, _fact()))), SecRevenueObservation)


def test_same_accession_pre_publication_receipt_is_not_hidden_by_duration() -> None:
    source = _capture(_body(_fact(start="2026-01-01")), received="2026-11-25T20:59:00Z")
    with pytest.raises(
        ValueError, match="CompanyFacts fact was received before witnessed filing publication"
    ):
        _read(source)

    source = replace(source, received_at=_instant("2026-11-25T20:54:00Z"))
    unknown_publication = replace(_FILING, published_at=None, publication_ref=None)
    with pytest.raises(ValueError, match="CompanyFacts fact was received before filing acceptance"):
        _read(source, filing=unknown_publication)


@pytest.mark.parametrize("asof", ["2026-11-25T21:12:00Z", "2028-01-01T00:00:00Z"])
def test_future_period_cannot_be_booked_by_an_earlier_pinned_filing(asof: str) -> None:
    end = date(2027, 9, 30)
    start = date(2027, 7, 1)
    source = _capture(_body(_fact(start=start.isoformat(), end=end.isoformat())))
    with pytest.raises(
        ValueError, match="SEC revenue period end is after the filing acceptance date bound"
    ):
        observe_sec_revenue(
            source,
            _FILING,
            expected_cik=_CIK,
            period_start=start,
            period_end=end,
            cutoff=_instant(asof),
        )


def test_received_invalid_bytes_and_issuer_are_not_hidden_by_unverified_publication() -> None:
    unknown_publication = replace(_FILING, published_at=None, publication_ref=None)
    valid_capture = _capture(_body(_fact()))
    assert _read(valid_capture, filing=unknown_publication) == "publication_unverified"

    for source, error in (
        (replace(valid_capture, body_sha256="f" * 64), "receipt SHA-256"),
        (_capture(_body(_fact(), cik=999999)), "CompanyFacts CIK"),
        (_capture(b"<html>blocked</html>"), "CompanyFacts JSON"),
        (
            _capture(b'{"cik":123456,"facts":{"us-gaap":{"Revenues":{"units":{"USD":null}}}}}'),
            "USD facts",
        ),
        (_capture(_body(_fact(val=True))), "SEC revenue value must be a JSON number"),
    ):
        with pytest.raises(ValueError, match=error):
            _read(source, filing=unknown_publication)
        not_received = replace(source, received_at=_instant("2026-11-26T00:00:00Z"))
        assert _read(not_received, filing=unknown_publication) == "not_yet_received"


def test_companyfacts_and_filing_cannot_share_one_pinned_receipt_reference() -> None:
    source = _capture(_body(_fact()))
    filing = replace(_FILING, receipt_id=source.receipt_id)
    with pytest.raises(
        ValueError, match="CompanyFacts and filing require distinct receipt references"
    ):
        _read(source, filing=filing)


def test_companyfacts_receipt_cannot_pass_as_independent_filing_publication_evidence() -> None:
    source = _capture(_body(_fact()))
    filing = replace(_FILING, publication_ref=source.receipt_id)
    with pytest.raises(ValueError, match="CompanyFacts receipt cannot witness filing publication"):
        _read(source, filing=filing)

    filing = replace(_FILING, publication_ref=_FILING.receipt_id)
    assert isinstance(_read(source, filing=filing), SecRevenueObservation)


@pytest.mark.parametrize("breakage", ["publication", "issuer", "time_order"])
def test_future_filing_metadata_does_not_change_prior_replay(breakage: str) -> None:
    source = _capture(_body(_fact()))
    future = replace(_FILING, received_at=_instant("2026-11-26T06:33:00Z"))
    assert _read(source, filing=future) == "not_yet_received"
    if breakage == "publication":
        future = replace(future, publication_ref=None)
        error = "publication time and witness must be supplied together"
    elif breakage == "issuer":
        future = replace(future, issuer_cik="0000999999")
        error = "filing witness CIK does not match the expected issuer"
    else:
        future = replace(future, accepted_at=_instant("2026-11-26T08:00:00Z"))
        error = "invalid SEC filing acceptance/publication/receipt order"

    assert _read(source, filing=future) == "not_yet_received"
    with pytest.raises(ValueError, match=error):
        _read(source, filing=future, cutoff=future.received_at)


def _index_proof(**changes):
    fields = {
        "issuer_cik": "123456",
        "accession": _ACCESSION,
        "observed_at": _instant("2026-11-25T22:10:00+01:00"),
        "receipt_id": "sec-index:fixed-synthetic-receipt",
        "index_url": (
            "https://www.sec.gov/Archives/edgar/data/123456/"
            "000099999926000001/0000999999-26-000001-index.htm"
        ),
    }
    return SecPublicFilingObservation(**(fields | changes))


def _read_observed(source, *, proof, cutoff, filing=None):
    return observe_sec_revenue(
        source,
        filing or replace(_FILING, published_at=None, publication_ref=None),
        expected_cik=_CIK,
        period_start=_Q_START,
        period_end=_Q_END,
        cutoff=cutoff,
        publicly_observed_by=proof,
    )


def test_public_index_observation_is_usable_only_from_its_independent_clock() -> None:
    source = _capture(_body(_fact()))  # 21:07Z, before the 21:10Z index receipt.
    proof = _index_proof()
    earlier = _instant("2026-11-25T21:09:59.999999Z")
    assert _read_observed(source, proof=proof, cutoff=earlier) == "publication_unverified"
    result = _read_observed(source, proof=proof, cutoff=proof.observed_at)
    assert isinstance(result, SecRevenueObservation)
    assert result.amount == Decimal(5000)
    assert result.filing.published_at is None
    assert result.filing.publication_ref is None
    assert result.companyfacts_received_at == source.received_at < proof.observed_at
    assert result.publicly_observed_by == replace(
        proof, issuer_cik=_CIK, observed_at=_instant("2026-11-25T21:10:00Z")
    )
    assert result.publicly_observed_by.observed_at.isoformat() == "2026-11-25T21:10:00+00:00"


@pytest.mark.parametrize("source_late", ["companyfacts", "filing"])
def test_public_index_never_substitutes_either_pinned_source_receipt(source_late: str) -> None:
    proof = _index_proof()
    late = _instant("2026-11-25T21:11:00Z")
    source = _capture(_body(_fact()))
    filing = replace(_FILING, published_at=None, publication_ref=None)
    if source_late == "companyfacts":
        source = replace(source, received_at=late)
    else:
        filing = replace(filing, received_at=late)
    assert (
        _read_observed(source, proof=proof, filing=filing, cutoff=proof.observed_at)
        == "not_yet_received"
    )
    assert isinstance(
        _read_observed(source, proof=proof, filing=filing, cutoff=late), SecRevenueObservation
    )


@pytest.mark.parametrize(
    ("changes", "error"),
    [
        ({"issuer_cik": "0000999999"}, "CIK"),
        ({"accession": _AMENDMENT}, "accession"),
        ({"index_url": "https://www.sec.gov/Archives/edgar/data/123456/nope"}, "index URL"),
        ({"receipt_id": "companyfacts-capture-a"}, "distinct"),
        ({"receipt_id": _FILING.receipt_id}, "distinct"),
        ({"receipt_id": " "}, "receipt"),
        ({"receipt_id": " companyfacts-capture-a "}, "receipt"),
        ({"observed_at": _instant("2026-11-25T20:54:59Z")}, "before filing acceptance"),
    ],
)
def test_wrong_public_observation_provenance_is_not_a_public_witness(changes, error) -> None:
    with pytest.raises(ValueError, match=error):
        _read_observed(_capture(_body(_fact())), proof=_index_proof(**changes), cutoff=_CUTOFF)


def test_future_index_metadata_cannot_change_old_proof_or_hide_invalid_body() -> None:
    proof = _index_proof(index_url="https://example.invalid/wrong")
    before = _instant("2026-11-25T21:09:59Z")
    source = _capture(_body(_fact()))
    assert _read_observed(source, proof=proof, cutoff=before) == "publication_unverified"
    observed_exact = _read_observed(source, proof=proof, cutoff=before, filing=_FILING)
    assert isinstance(observed_exact, SecRevenueObservation)
    assert observed_exact.filing.published_at == _FILING.published_at
    assert observed_exact.publicly_observed_by is None
    with pytest.raises(ValueError, match="receipt SHA-256"):
        _read_observed(replace(source, body=b"corrupt"), proof=proof, cutoff=before)


def test_optional_observation_cannot_weaken_exact_publication_chronology() -> None:
    source = _capture(_body(_fact()), received="2026-11-25T20:59:00Z")
    with pytest.raises(ValueError, match="received before witnessed filing publication"):
        _read_observed(source, proof=_index_proof(), cutoff=_CUTOFF, filing=_FILING)
    impossible_proof = _index_proof(observed_at=_instant("2026-11-25T20:58:00Z"))
    with pytest.raises(ValueError, match="observation is before witnessed filing publication"):
        _read_observed(
            _capture(_body(_fact())), proof=impossible_proof, cutoff=_CUTOFF, filing=_FILING
        )


def test_original_index_can_be_observed_at_filing_acceptance() -> None:
    proof = _index_proof(observed_at=_FILING.accepted_at)
    result = _read_observed(_capture(_body(_fact())), proof=proof, cutoff=_CUTOFF)
    assert isinstance(result, SecRevenueObservation)
    assert result.publicly_observed_by.observed_at == _FILING.accepted_at


@pytest.mark.parametrize("counterpart", ["companyfacts", "filing", "publication"])
def test_index_receipt_cannot_alias_a_padded_legacy_reference(counterpart: str) -> None:
    proof = _index_proof()
    padded = f" {proof.receipt_id}\t "
    source = _capture(_body(_fact()))
    filing = _FILING
    if counterpart == "companyfacts":
        source = replace(source, receipt_id=padded)
    elif counterpart == "filing":
        filing = replace(filing, receipt_id=padded)
    else:
        filing = replace(filing, publication_ref=padded)

    # Existing exact-evidence-only route has no new reference restrictions.
    assert isinstance(_read(source, filing=filing), SecRevenueObservation)
    with pytest.raises(ValueError, match="distinct original index receipt"):
        _read_observed(source, proof=proof, cutoff=_CUTOFF, filing=filing)
