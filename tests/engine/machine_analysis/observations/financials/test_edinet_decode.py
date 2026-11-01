"""Canonical EDINET metric decoding, independent of native filing metadata."""

import pytest

from baibai_engine.machine_analysis.observations.financials.edinet_decode import (
    is_usable_filing_status,
    normalize_metric_record,
)


@pytest.mark.parametrize(
    "reasons",
    ["metric_unavailable:cash", ["metric_unavailable:cash"], ("metric_unavailable:cash",)],
)
def test_metric_failure_reason_is_not_split_into_characters(reasons):
    record = normalize_metric_record({"ticker": "7203", "failure_reasons": reasons})
    assert record.failure_reasons == ("metric_unavailable:cash",)


def test_canonical_metric_zero_and_missing_values_are_preserved():
    record = normalize_metric_record(
        {"ticker": "7203", "cash": 0, "debt": None, "sales_ttm": "100"}
    )
    assert record.cash == 0
    assert record.debt is None
    assert record.sales_ttm == 100


@pytest.mark.parametrize("legal", ["1", "2", 1, 2])
def test_explicit_published_filing_is_usable(legal):
    assert is_usable_filing_status(legal, "0", "0")


@pytest.mark.parametrize(
    "flags",
    [
        (None, "0", "0"),
        ("1", None, "0"),
        ("1", "0", None),
        ("", "0", "0"),
        ("1", "", "0"),
        ("1", "0", ""),
        ("0", "0", "0"),
        ("1", "1", "0"),
        ("1", "0", "1"),
    ],
)
def test_missing_or_unavailable_filing_status_is_not_usable(flags):
    assert not is_usable_filing_status(*flags)
