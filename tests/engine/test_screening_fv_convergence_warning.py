from __future__ import annotations

from collections.abc import Mapping
from datetime import date
from math import inf, nan

import pytest

from baibai_engine.screening.candidate_build import build_screened_candidate
from baibai_engine.screening.render import candidate_entry
from baibai_engine.screening.rule_config import DEFAULT_RULES_PATH, load_screening_rules
from baibai_engine.screening.schema import (
    DerivedMetrics,
    FinancialSnapshot,
    SecurityMaster,
    UniverseSnapshot,
)
from baibai_engine.screening.selection import build_selection_payload, candidate_record_from_mapping
from baibai_engine.screening.selection.summaries import (
    _fv_convergence_annotation,
    _screening_reference_close_yen,
)

_ASOF = date(2026, 7, 29)


def _annotation(
    *,
    price: object = 100.0,
    sector_anchor: object = 90.0,
    self_anchor: object = 95.0,
    reversion: object = -0.01,
) -> Mapping[str, object]:
    candidate = {"market_cap_oku": 90.0}
    metrics = {
        "market_price_yen": price,
        "shares_outstanding": 100_000_000,
        "fv_sector_median_yen": sector_anchor,
        "fv_self_range_yen": self_anchor,
        "er_reversion_annual": reversion,
    }
    return _fv_convergence_annotation(candidate, metrics)


def test_reference_price_is_not_reversed_from_treasury_adjusted_market_cap() -> None:
    metrics = {"shares_outstanding": 100_000_000, "market_price_yen": 100.0}

    assert _screening_reference_close_yen(metrics) == 100.0


def test_observed_price_survives_candidate_serialization_and_selection() -> None:
    financial = FinancialSnapshot(
        latest_disclosed_at=None,
        per_forward=None,
        per_trailing=None,
        pbr=0.8,
        ev_ebitda=None,
        p_s=None,
        pcfr=None,
        eps=None,
        sales_ttm=None,
        ocf_ttm=None,
        market_price_yen=100.0,
        shares_outstanding=100_000_000.0,
        shares_ex_treasury=90_000_000.0,
        market_cap=9_000_000_000.0,
    )
    candidate = build_screened_candidate(
        ticker="1111",
        security=SecurityMaster(
            code="1111",
            name="raw-close-regression",
            market_segment="Prime",
            sector_33="機械",
            is_common_stock=True,
        ),
        financial=financial,
        derived=DerivedMetrics(sector_median_value={"pbr": 1.2}),
        universe_snapshot=UniverseSnapshot(
            market_cap_oku=500,
            avg_turnover_oku=2.0,
            listing_span_days=1_200,
        ),
        evidence_hits=(),
    )
    serialized = candidate_entry(candidate)
    assert serialized["metrics"]["market_price_yen"] == 100.0
    assert serialized["metrics"]["fv_sector_median_yen"] == 150.0

    payload = build_selection_payload(
        asof_date=_ASOF,
        candidates=(candidate_record_from_mapping(serialized),),
        macro_context=None,
        rules=load_screening_rules(DEFAULT_RULES_PATH),
        candidates_ref="test.yaml",
        macro_context_ref=None,
        review_cap=1,
    )
    assert payload["ranked_set"][0]["market_price_yen"] == 100.0
    assert payload["ranked_set"][0]["fair_value_anchor_yen"] == 150.0
    assert payload["ranked_set"][0]["fv_convergence"]["market_price_yen"] == 100.0

    old_metrics = serialized["metrics"]
    assert isinstance(old_metrics, dict)
    del old_metrics["market_price_yen"]
    # actual old producer は net market cap / gross shares = 90 円を FV の基準にした。
    old_metrics["fv_sector_median_yen"] = 135.0
    old_payload = build_selection_payload(
        asof_date=_ASOF,
        candidates=(candidate_record_from_mapping(serialized),),
        macro_context=None,
        rules=load_screening_rules(DEFAULT_RULES_PATH),
        candidates_ref="old-test.yaml",
        macro_context_ref=None,
        review_cap=1,
    )
    assert old_payload["ranked_set"][0]["market_price_yen"] is None
    assert old_payload["ranked_set"][0]["fair_value_anchor_yen"] is None
    assert old_payload["ranked_set"][0]["fv_convergence"]["status"] == "not_evaluable"
    assert old_payload["ranked_set"][0]["fv_convergence"]["anchors_yen"] == {}
    assert old_payload["ranked_set"][0]["estimate_snapshot"]["fair_value"]["anchors"] == {}
    assert old_payload["ranked_set"][0]["fv_sector_median_yen"] is None
    assert (
        old_payload["ranked_set"][0]["decision_input_seed"]["estimates"]["fair_value"]["anchors"]
        == {}
    )


def test_one_anchor_and_two_anchor_boundaries_are_explicit() -> None:
    one_anchor = _annotation(sector_anchor=100.0, self_anchor=None, reversion=0.0)
    both_anchors = _annotation(sector_anchor=90.0, self_anchor=100.0, reversion=0.0)
    one_still_above_price = _annotation(sector_anchor=90.0, self_anchor=100.01, reversion=-0.01)

    assert one_anchor["status"] == "warning"
    assert one_anchor["anchors_yen"] == {"fv_sector_median_yen": 100.0}
    assert both_anchors["status"] == "warning"
    assert one_still_above_price["status"] == "clear"


def test_positive_reversion_prevents_warning_even_after_both_displayed_anchors() -> None:
    result = _annotation(sector_anchor=90.0, self_anchor=95.0, reversion=0.001)

    assert result["status"] == "clear"
    assert result["warning_code"] is None


@pytest.mark.parametrize("value", [None, True, "unknown", nan, inf, -inf])
def test_missing_or_nonfinite_reversion_is_not_evaluable(value: object) -> None:
    assert _annotation(reversion=value)["status"] == "not_evaluable"


@pytest.mark.parametrize("value", [None, True, "unknown", nan, inf, -inf, 0.0, -1.0])
def test_invalid_price_is_not_evaluable(value: object) -> None:
    assert _annotation(price=value)["status"] == "not_evaluable"


@pytest.mark.parametrize("value", [None, True, "unknown", nan, inf, -inf, 0.0, -1.0])
def test_invalid_anchor_is_never_zero_filled(value: object) -> None:
    no_anchor = _annotation(sector_anchor=value, self_anchor=value)
    one_valid_anchor = _annotation(sector_anchor=value, self_anchor=90.0)

    assert no_anchor["status"] == "not_evaluable"
    assert no_anchor["anchors_yen"] == {}
    assert one_valid_anchor["status"] == "warning"
    assert one_valid_anchor["anchors_yen"] == {"fv_self_range_yen": 90.0}


def _candidate(
    ticker: str,
    *,
    er_annual: float,
    anchor: float,
    reversion: float,
) -> Mapping[str, object]:
    return {
        "ticker": ticker,
        "name": f"name-{ticker}",
        "sector_33": "機械",
        "market_cap_oku": 500.0,
        "avg_turnover_oku": 2.0,
        "listing_span_days": 1200,
        "jpx_flags": [],
        "evidence_hits": [],
        "metrics": {
            "ocf_yield": 0.1,
            "market_price_yen": 500.0,
            "shares_outstanding": 100_000_000,
            "er_annual": er_annual,
            "er_reversion_annual": reversion,
            "fv_sector_median_yen": anchor,
        },
    }


def test_warning_is_ranked_set_annotation_and_does_not_change_er_ranking() -> None:
    payload = build_selection_payload(
        asof_date=_ASOF,
        candidates=tuple(
            candidate_record_from_mapping(candidate)
            for candidate in (
                _candidate("1111", er_annual=0.12, anchor=450.0, reversion=-0.01),
                _candidate("2222", er_annual=0.08, anchor=550.0, reversion=0.01),
            )
        ),
        macro_context=None,
        rules=load_screening_rules(DEFAULT_RULES_PATH),
        candidates_ref="test.yaml",
        macro_context_ref=None,
        review_cap=2,
    )

    ranked_set = payload["ranked_set"]
    assert isinstance(ranked_set, list)
    assert [item["ticker"] for item in ranked_set] == ["1111", "2222"]
    assert [item["expected_return_pct"] for item in ranked_set] == [12.0, 8.0]
    assert ranked_set[0]["fv_convergence"]["status"] == "warning"
    assert ranked_set[1]["fv_convergence"]["status"] == "clear"


@pytest.mark.parametrize(
    ("price", "sector_anchor", "self_anchor", "reversion", "expected"),
    [
        (1278.7500, 2008.4240, 1238.7500, -0.0031, "clear"),
        (1914.2786, 2742.2613, 1823.3333, -0.0047, "clear"),
        (1390.7387, 1587.4772, 1279.0000, -0.0080, "clear"),
        (1213.7684, 1851.5249, 976.0000, -0.0196, "clear"),
        (3788.7029, 3313.2775, 3940.5000, -0.0127, "clear"),
        (8514.3645, 7111.3813, 11975.0000, -0.0165, "clear"),
        (1773.9109, 2535.5529, 1716.2500, -0.0033, "clear"),
        (1367.4614, 1304.2200, 1313.5000, -0.0108, "warning"),
        (3766.2014, 3238.3871, 3945.5000, -0.0140, "clear"),
        (2812.6550, 2864.7519, 2919.5000, 0.0018, "clear"),
        (3895.5854, 3154.7924, 3945.5000, -0.0190, "clear"),
        (4040.5319, 3756.2200, 3905.0000, -0.0168, "warning"),
    ],
)
def test_price_already_converged_retrospective_fixture(
    price: float,
    sector_anchor: float,
    self_anchor: float,
    reversion: float,
    expected: str,
) -> None:
    assert (
        _annotation(
            price=price,
            sector_anchor=sector_anchor,
            self_anchor=self_anchor,
            reversion=reversion,
        )["status"]
        == expected
    )
