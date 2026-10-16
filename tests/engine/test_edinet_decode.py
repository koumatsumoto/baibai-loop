"""Canonical EDINET metric decoding, independent of native filing metadata."""

import pytest

from baibai_engine.market.edinet_decode import normalize_metric_record


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
