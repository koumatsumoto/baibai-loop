from __future__ import annotations

from copy import deepcopy

import pytest

from baibai_engine.screening.discovery.review_set import (
    ReviewSetContractError,
    build_review_set,
    candidate_discovery_method_hash,
    validate_review_set_payload,
    validate_review_set_shape,
)
from baibai_engine.screening.rule_config import load_screening_rules

SCREENING_RULES = load_screening_rules()
RULES = SCREENING_RULES.candidate_discovery
REQUIRED_JPX_FLAGS = SCREENING_RULES.universe.required_jpx_flags


def _build_review_set(
    rows: list[dict[str, object]],
    *,
    judged_through_research_triage_id: str | None = None,
) -> dict[str, object]:
    return build_review_set(
        rows,
        rules=RULES,
        required_jpx_flags=REQUIRED_JPX_FLAGS,
        judged_through_research_triage_id=judged_through_research_triage_id,
    )


def _analysis(
    ticker: str,
    *,
    per: float = 10.0,
    normalized_per: float = 10.0,
    asset_ratio: float = 0.5,
    p_s: float = 1.0,
) -> dict[str, object]:
    return {
        "ticker": ticker,
        "name": ticker,
        "sector_33": "情報・通信業",
        "market_cap_oku": 500,
        "avg_turnover_oku": 5.0,
        "listing_span_days": 1000,
        "jpx_flags": [],
        "per_forward": per,
        "per_trailing": per + 1,
        "pbr": 0.8,
        "p_s": p_s,
        "ev_ebitda": 5.0,
        "pcfr": 8.0,
        "metrics": {
            "per_forward_sector_gap": per / 20.0 - 1.0,
            "normalized_per_3fy": normalized_per,
            "fcf_yield": 0.08,
            "ocf_yield": 0.10,
            "asset_backed_ratio": asset_ratio,
            "net_cash_to_market_cap": 0.25,
            "pbr_sector_gap": -0.3,
            "equity_ratio": 0.6,
            "p_s_sector_gap": -0.4,
            "sales_yoy": 0.05,
            "operating_profit": 12.0,
            "sales_ttm": 100.0,
            "total_assets": 200.0,
            "debt": 20.0,
            "cash": 30.0,
            "er_annual": 0.1,
        },
    }


def test_overlap_is_selected_once_and_covers_every_supported_target() -> None:
    rows = [_analysis(str(1000 + index), per=5.0 + index) for index in range(20)]

    payload = _build_review_set(rows)

    entries = payload["entries"]
    assert isinstance(entries, list)
    assert len(entries) == 20
    assert entries[0]["support_count"] == 4
    assert len({entry["ticker"] for entry in entries}) == 20
    diagnostics = payload["diagnostics"]
    assert diagnostics["unfilled_representation_targets"] == {
        "current-earnings-power": 0,
        "normalized-earnings-power": 0,
        "asset-value": 0,
        "reinvestment-value": 0,
    }


def test_expected_return_and_context_do_not_change_membership_or_order() -> None:
    rows = [_analysis(str(1000 + index), per=5.0 + index) for index in range(21)]
    baseline = _build_review_set(rows)
    changed = deepcopy(rows)
    for index, row in enumerate(changed):
        row["metrics"]["er_annual"] = -99.0 + index  # type: ignore[index]
        row["metrics"]["tender_offer_event_recent"] = index % 2 == 0  # type: ignore[index]

    result = _build_review_set(changed)

    assert [entry["ticker"] for entry in result["entries"]] == [
        entry["ticker"] for entry in baseline["entries"]
    ]


def test_research_triage_head_does_not_change_membership_or_order() -> None:
    rows = [_analysis(str(1000 + index), per=5.0 + index) for index in range(20)]
    baseline = _build_review_set(rows)

    result = _build_review_set(
        rows,
        judged_through_research_triage_id="research-triage-head",
    )

    assert result["review_basis"] == {"judged_through_research_triage_id": "research-triage-head"}
    assert result["entries"] == baseline["entries"]
    assert result["diagnostics"] == baseline["diagnostics"]


def test_recomputed_validation_rejects_changed_membership() -> None:
    rows = [_analysis(str(1000 + index), per=5.0 + index) for index in range(20)]
    payload = _build_review_set(rows)
    payload["entries"] = list(reversed(payload["entries"]))

    with pytest.raises(ReviewSetContractError):
        validate_review_set_payload(
            payload,
            security_analyses=rows,
            rules=RULES,
            required_jpx_flags=REQUIRED_JPX_FLAGS,
        )


def test_null_and_nonpositive_primary_coordinates_do_not_nominate() -> None:
    row = _analysis("1111")
    row["per_forward"] = None
    row["per_trailing"] = -1.0
    row["metrics"]["normalized_per_3fy"] = None  # type: ignore[index]
    row["metrics"]["asset_backed_ratio"] = None  # type: ignore[index]
    row["metrics"]["net_cash_to_market_cap"] = 0.0  # type: ignore[index]
    row["metrics"]["sales_yoy"] = 0.0  # type: ignore[index]

    payload = _build_review_set([row])

    assert payload["entries"] == []


def test_normalized_gap_uses_the_shared_sector_population_boundary() -> None:
    rows = [
        {
            **_analysis(str(1000 + index), normalized_per=100.0 + index),
            "sector_33": "thin-sector",
        }
        for index in range(9)
    ]
    rows.extend(
        {
            **_analysis(str(2000 + index), normalized_per=10.0 + index),
            "sector_33": "large-sector",
        }
        for index in range(10)
    )

    payload = _build_review_set(rows)

    entries = payload["entries"]
    thin = next(entry for entry in entries if entry["ticker"] == "1000")
    # A nine-security sector falls back to the 19-security market median.
    assert thin["analysis"]["normalized_earnings"][
        "normalized_per_3fy_sector_gap"
    ] == pytest.approx(100.0 / 19.0 - 1.0)
    thick = next(entry for entry in entries if entry["ticker"] == "2000")
    # At the shared boundary, the ten-security sector uses its own median.
    assert thick["analysis"]["normalized_earnings"][
        "normalized_per_3fy_sector_gap"
    ] == pytest.approx(10.0 / 14.5 - 1.0)


@pytest.mark.parametrize(
    ("flags", "expected"),
    [
        ([], True),
        (["特別注意銘柄"], False),
        (["情報提供のみ"], True),
        (None, False),
    ],
)
def test_review_set_uses_the_shared_required_jpx_flag_contract(
    flags: list[str] | None,
    expected: bool,
) -> None:
    row = _analysis("1111")
    row["jpx_flags"] = flags

    payload = _build_review_set([row])

    assert bool(payload["entries"]) is expected


def test_candidate_discovery_hash_binds_the_required_jpx_flag_set() -> None:
    baseline = candidate_discovery_method_hash(
        RULES,
        required_jpx_flags=("整理銘柄", "取引停止"),
    )

    assert baseline == candidate_discovery_method_hash(
        RULES,
        required_jpx_flags=("取引停止", "整理銘柄"),
    )
    assert baseline != candidate_discovery_method_hash(
        RULES,
        required_jpx_flags=("整理銘柄",),
    )


@pytest.mark.parametrize(
    "mutate",
    [
        lambda analysis: analysis.update({"unknown_group": {}}),
        lambda analysis: analysis.pop("current_earnings"),
        lambda analysis: analysis["identity_liquidity"].update({"market_cap_oku": "500"}),
        lambda analysis: analysis["valuation"].update({"unknown_metric": 1.0}),
    ],
)
def test_review_set_analysis_rejects_nested_contract_drift(mutate) -> None:
    payload = _build_review_set([_analysis("1111")])
    entry = payload["entries"][0]
    mutate(entry["analysis"])

    with pytest.raises(ReviewSetContractError, match="review set entry is invalid"):
        validate_review_set_shape(payload)
