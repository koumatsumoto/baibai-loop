from __future__ import annotations

from copy import deepcopy

import pytest

from baibai_engine.foundation.candidate_discovery import Nomination
from baibai_engine.screening.discovery import review_set as review_set_module
from baibai_engine.screening.discovery.review_set import (
    ReviewSetContractError,
    build_nomination_ranks,
    build_review_set,
    candidate_discovery_method_hash,
    validate_review_set_payload,
    validate_review_set_shape,
)
from baibai_engine.screening.rule_config import load_screening_rules

SCREENING_RULES = load_screening_rules()
RULES = SCREENING_RULES.candidate_discovery
REQUIRED_JPX_FLAGS = SCREENING_RULES.universe.required_jpx_flags


def _build_review_set(rows: list[dict[str, object]]) -> dict[str, object]:
    return build_review_set(
        rows,
        rules=RULES,
        required_jpx_flags=REQUIRED_JPX_FLAGS,
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


def test_nomination_union_contains_each_ticker_once_with_every_nomination() -> None:
    rows = [_analysis(str(1000 + index), per=5.0 + index) for index in range(20)]

    payload = _build_review_set(rows)

    entries = payload["entries"]
    assert isinstance(entries, list)
    assert len(entries) == 20
    assert len(entries[0]["nominations"]) == 4
    assert len({entry["ticker"] for entry in entries}) == 20
    diagnostics = payload["diagnostics"]
    assert diagnostics["unique_candidate_count"] == len(entries)


def test_nomination_union_can_reach_four_disjoint_top20_sets(monkeypatch) -> None:
    rows = [_analysis(str(1000 + index)) for index in range(80)]
    nominations = {
        str(1000 + approach_index * 20 + rank - 1): (
            Nomination(
                valuation_approach_id=approach,
                valuation_method_id=RULES.approaches[approach].method_id,
                rank=rank,
            ),
        )
        for approach_index, approach in enumerate(review_set_module.APPROACH_IDS)
        for rank in range(1, 21)
    }
    monkeypatch.setattr(review_set_module, "_nomination_ranks", lambda *args, **kwargs: nominations)

    payload = _build_review_set(rows)

    entries = payload["entries"]
    assert len(entries) == 80
    assert {entry["ticker"] for entry in entries} == set(nominations)
    assert payload["diagnostics"]["nomination_counts"] == dict.fromkeys(
        review_set_module.APPROACH_IDS, 20
    )


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


@pytest.mark.parametrize("avg_turnover_oku", [0.1, None])
def test_adv_does_not_change_candidate_membership(avg_turnover_oku: float | None) -> None:
    row = _analysis("1111")
    row["avg_turnover_oku"] = avg_turnover_oku

    payload = _build_review_set([row])

    assert [entry["ticker"] for entry in payload["entries"]] == ["1111"]
    assert (
        payload["entries"][0]["analysis"]["identity_liquidity"]["avg_turnover_oku"]
        == avg_turnover_oku
    )


def test_composition_has_no_research_triage_state() -> None:
    rows = [_analysis(str(1000 + index), per=5.0 + index) for index in range(20)]
    baseline = _build_review_set(rows)

    assert "review_" + "basis" not in baseline


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


@pytest.mark.parametrize(
    "financial_sector",
    ["銀行業", "保険業", "その他金融業", "証券・商品先物取引業"],
)
def test_asset_value_excludes_financial_sectors_and_net_cash_only_rows(
    financial_sector: str,
) -> None:
    financial = _analysis("1111", asset_ratio=9.0)
    financial["sector_33"] = financial_sector
    net_cash_only = _analysis("2222", asset_ratio=0.0)
    net_cash_only["metrics"]["net_cash_to_market_cap"] = 9.0  # type: ignore[index]
    native = _analysis("3333", asset_ratio=0.5)

    payload = _build_review_set([financial, net_cash_only, native])
    entries = {entry["ticker"]: entry for entry in payload["entries"]}

    assert not any(
        nomination["valuation_approach_id"] == "asset-value"
        for nomination in entries["1111"]["nominations"]
    )
    assert not any(
        nomination["valuation_approach_id"] == "asset-value"
        for nomination in entries["2222"]["nominations"]
    )
    assert entries["2222"]["analysis"]["asset_value"]["net_cash_to_market_cap"] == 9.0
    assert any(
        nomination["valuation_approach_id"] == "asset-value"
        for nomination in entries["3333"]["nominations"]
    )


def test_asset_value_order_uses_pbr_before_net_cash_context() -> None:
    cheaper_pbr = _analysis("1111", asset_ratio=0.5)
    cheaper_pbr["metrics"]["pbr_sector_gap"] = -0.5  # type: ignore[index]
    cheaper_pbr["metrics"]["net_cash_to_market_cap"] = 0.01  # type: ignore[index]
    higher_net_cash = _analysis("2222", asset_ratio=0.5)
    higher_net_cash["metrics"]["pbr_sector_gap"] = -0.1  # type: ignore[index]
    higher_net_cash["metrics"]["net_cash_to_market_cap"] = 10.0  # type: ignore[index]

    nominations = build_nomination_ranks(
        [higher_net_cash, cheaper_pbr],
        rules=RULES,
        required_jpx_flags=REQUIRED_JPX_FLAGS,
    )

    asset_ranks = {
        ticker: next(
            item.rank for item in ticker_nominations if item.valuation_approach_id == "asset-value"
        )
        for ticker, ticker_nominations in nominations.items()
    }
    assert asset_ranks == {"1111": 1, "2222": 2}


def test_reinvestment_requires_cheap_ps_and_both_sector_quality_floors() -> None:
    rows = [_analysis(str(1000 + index)) for index in range(10)]
    expensive = _analysis("2001")
    expensive["metrics"]["p_s_sector_gap"] = 0.0  # type: ignore[index]
    low_margin = _analysis("2002")
    low_margin["metrics"]["sales_ttm"] = 1000.0  # type: ignore[index]
    low_capital_return = _analysis("2003")
    low_capital_return["metrics"]["total_assets"] = 2000.0  # type: ignore[index]
    rows.extend((expensive, low_margin, low_capital_return))

    nominations = build_nomination_ranks(
        rows,
        rules=RULES,
        required_jpx_flags=REQUIRED_JPX_FLAGS,
    )

    for ticker in ("2001", "2002", "2003"):
        assert not any(
            item.valuation_approach_id == "reinvestment-value" for item in nominations[ticker]
        )


def test_reinvestment_sector_floor_uses_inclusive_shared_population_boundary() -> None:
    low_sector = [
        {
            **_analysis(str(1000 + index)),
            "sector_33": "boundary-sector",
        }
        for index in range(10)
    ]
    high_sector = []
    for index in range(10):
        row = _analysis(str(2000 + index))
        row["sector_33"] = "high-sector"
        row["metrics"]["operating_profit"] = 100.0  # type: ignore[index]
        high_sector.append(row)

    at_boundary = build_nomination_ranks(
        [*low_sector, *high_sector],
        rules=RULES,
        required_jpx_flags=REQUIRED_JPX_FLAGS,
    )
    below_boundary = build_nomination_ranks(
        [*low_sector[:-1], *high_sector],
        rules=RULES,
        required_jpx_flags=REQUIRED_JPX_FLAGS,
    )

    assert all(
        any(
            item.valuation_approach_id == "reinvestment-value"
            for item in at_boundary[row["ticker"]]
        )
        for row in low_sector
    )
    assert all(
        not any(
            item.valuation_approach_id == "reinvestment-value"
            for item in below_boundary[row["ticker"]]
        )
        for row in low_sector[:-1]
    )


def test_current_rules_name_only_the_two_revised_approaches() -> None:
    method_ids = {key: value.method_id for key, value in RULES.approaches.items()}

    assert RULES.method_id == "multi-valuation-v4"
    assert method_ids == {
        "current-earnings-power": "current-earnings-power-v1",
        "normalized-earnings-power": "normalized-earnings-power-v1",
        "asset-value": "asset-value-v2",
        "reinvestment-value": "reinvestment-value-v2",
    }
    assert RULES.nomination_depth == 20
    assert "min_avg_turnover_oku" not in RULES.common_eligibility.model_dump()


@pytest.mark.parametrize(("market_cap_oku", "expected"), [(99.999, False), (100.0, True)])
def test_common_eligibility_owns_the_100_oku_market_cap_boundary(
    market_cap_oku: float, expected: bool
) -> None:
    assert (
        RULES.common_eligibility.matches(
            market_cap_oku=market_cap_oku,
            listing_span_days=1000,
            jpx_flags=[],
            required_jpx_flags=REQUIRED_JPX_FLAGS,
            require_facts=True,
        )
        is expected
    )


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


def test_candidate_discovery_hash_binds_v2_sector_and_median_policy(monkeypatch) -> None:
    baseline = candidate_discovery_method_hash(
        RULES,
        required_jpx_flags=REQUIRED_JPX_FLAGS,
    )
    sectors = review_set_module._ASSET_VALUE_EXCLUDED_SECTORS
    minimum = review_set_module.MIN_SECTOR_MEDIAN_POPULATION

    monkeypatch.setattr(review_set_module, "_ASSET_VALUE_EXCLUDED_SECTORS", frozenset())
    assert baseline != candidate_discovery_method_hash(
        RULES,
        required_jpx_flags=REQUIRED_JPX_FLAGS,
    )

    monkeypatch.setattr(review_set_module, "_ASSET_VALUE_EXCLUDED_SECTORS", sectors)
    monkeypatch.setattr(review_set_module, "MIN_SECTOR_MEDIAN_POPULATION", minimum + 1)
    assert baseline != candidate_discovery_method_hash(
        RULES,
        required_jpx_flags=REQUIRED_JPX_FLAGS,
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
    payload.update(
        {
            "review_set_id": "review-set-test",
            "run_revision_id": "run-test",
            "as_of": "2026-07-19",
            "created_at": "2026-07-19T15:00:00+09:00",
            "screening_rules_hash": "a" * 64,
        }
    )
    entry = payload["entries"][0]
    mutate(entry["analysis"])

    with pytest.raises(ReviewSetContractError, match="published review set is invalid"):
        validate_review_set_shape(payload)


@pytest.mark.parametrize("mutation", ["unknown_approach", "rank_gap", "union_bound"])
def test_review_set_shape_rejects_invalid_nomination_union(mutation: str) -> None:
    payload = _build_review_set([_analysis("1111")])
    payload.update(
        {
            "review_set_id": "review-set-test",
            "run_revision_id": "run-test",
            "as_of": "2026-07-19",
            "created_at": "2026-07-19T15:00:00+09:00",
            "screening_rules_hash": "a" * 64,
        }
    )
    if mutation == "unknown_approach":
        payload["entries"][0]["nominations"][0]["valuation_approach_id"] = "unknown"
    elif mutation == "rank_gap":
        payload["entries"][0]["nominations"][0]["rank"] = 2
    else:
        payload["method"]["nomination_depth"] = 1
        template = payload["entries"][0]
        payload["entries"] = [
            {**deepcopy(template), "ticker": str(1111 + offset)} for offset in range(5)
        ]

    with pytest.raises(ReviewSetContractError, match="published review set is invalid"):
        validate_review_set_shape(payload)
