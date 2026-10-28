from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path
from random import Random

import pytest

from baibai_engine.machine_analysis.historical_evaluation.legacy_screening.discovery import (
    candidates as candidates_module,
)
from baibai_engine.machine_analysis.historical_evaluation.legacy_screening.discovery.candidates import (
    CandidateDiscoveryContractError,
    build_candidate_contexts,
    candidate_discovery_method_hash,
)
from baibai_engine.machine_analysis.historical_evaluation.legacy_screening.discovery.contracts import (
    APPROACH_IDS,
    CandidateDiscoveryResult,
    StockCandidateContext,
    validate_candidate_run,
)
from baibai_engine.machine_analysis.historical_evaluation.legacy_screening.discovery.projection import (
    candidate_rows,
    reinvestment_coordinates,
)
from baibai_engine.machine_analysis.historical_evaluation.legacy_screening.rule_config import (
    load_screening_rules,
)

SCREENING_RULES = load_screening_rules()
RULES = SCREENING_RULES.candidate_discovery
REQUIRED_JPX_FLAGS = SCREENING_RULES.universe.required_jpx_flags
GOLDEN = json.loads(Path("tests/fixtures/screening/candidate-discovery-golden.json").read_text())


def _discover(rows: list[dict[str, object]]) -> CandidateDiscoveryResult:
    return build_candidate_contexts(rows, rules=RULES, required_jpx_flags=REQUIRED_JPX_FLAGS)


def _ranks(result: CandidateDiscoveryResult, approach: str) -> dict[str, int]:
    return {
        ticker: reason.rank
        for ticker, context in result.contexts.items()
        for reason in context.reasons
        if reason.valuation_approach_id == approach
    }


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
        "price_history_span_days": 1000,
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
            "operating_profit_ttm": 12.0,
            "sales_ttm": 100.0,
            "total_assets": 200.0,
            "same_state_equity_yen": 120.0,
            "debt": 20.0,
            "cash": 30.0,
            "er_annual": 0.1,
        },
    }


def test_golden_preserves_frozen_membership_ranks_gaps_and_method_identity() -> None:
    # Frozen inputs exercise Discovery, not valuation or forward-cache reconstruction.
    result = _discover(GOLDEN["rows"])
    assert result.method.model_dump(mode="json") == GOLDEN["method"]
    assert {
        ticker: [item.model_dump(mode="json") for item in context.reasons]
        for ticker, context in result.contexts.items()
        if context.reasons
    } == GOLDEN["reasons"]
    assert {
        ticker: result.contexts[ticker].normalized_per_3fy_sector_gap
        for ticker in GOLDEN["normalized_gaps"]
    } == GOLDEN["normalized_gaps"]
    assert result.common_eligible_count == GOLDEN["common_eligible_count"]
    assert result.candidate_counts_by_approach == GOLDEN["counts"]
    assert result.candidate_count == GOLDEN["candidate_count"]


@pytest.mark.parametrize("approach", APPROACH_IDS)
def test_top20_capacity_inclusive_ties_and_ticker_tiebreak(approach: str) -> None:
    rows = [_analysis(str(1000 + index)) for index in reversed(range(21))]
    result = _discover(rows)
    assert _ranks(result, approach) == {str(1000 + index): index + 1 for index in range(20)}
    assert result.contexts["1020"].common_eligible
    assert result.contexts["1020"].reasons == ()
    assert result.contexts["1020"].normalized_per_3fy_sector_gap == 0.0
    assert result.candidate_count == 20
    assert all(len(result.contexts[str(1000 + index)].reasons) == 4 for index in range(20))


def test_all_rows_covered_shuffling_unchanged_and_projection_keeps_raw_values() -> None:
    rows = deepcopy(GOLDEN["rows"])
    baseline = _discover(rows)
    Random(1532).shuffle(rows)
    result = _discover(rows)
    assert result == baseline
    finalized = [
        {**row, "candidate": result.contexts[row["ticker"]].model_dump(mode="json")} for row in rows
    ]
    projected = candidate_rows(finalized)
    assert [row["ticker"] for row in projected] == sorted(GOLDEN["reasons"])
    assert all(any(row is original for original in finalized) for row in projected)
    assert set(result.contexts) == {row["ticker"] for row in rows}
    for ticker in ("9001", "9002", "9003"):
        assert result.contexts[ticker] == StockCandidateContext(
            common_eligible=False, reasons=(), normalized_per_3fy_sector_gap=None
        )
    with pytest.raises(TypeError):
        result.contexts["1111"] = result.contexts["9001"]


@pytest.mark.parametrize("avg_turnover_oku", [0.1, 0.0, None])
def test_adv_and_expected_return_context_do_not_change_selection(avg_turnover_oku) -> None:
    rows = [_analysis(str(1000 + index), per=5.0 + index) for index in range(21)]
    baseline = _discover(rows)
    changed = deepcopy(rows)
    for index, row in enumerate(changed):
        row["avg_turnover_oku"] = avg_turnover_oku
        row["metrics"].update(er_annual=-99.0 + index, tender_offer_event_recent=bool(index % 2))
        row["ttm_quality"] = {"ev_ebitda": "unavailable", "fcf_yield": "approximated"}
    assert _discover(changed) == baseline


@pytest.mark.parametrize(
    ("span", "flags", "expected"),
    [
        (0, [], True),
        (20, [], True),
        (181, [], True),
        (182, [], True),
        (-1, [], False),
        (None, [], False),
        (float("nan"), [], False),
        (float("inf"), [], False),
        (20, ["取引停止"], False),
        (20, None, False),
        (20, ["特別注意銘柄"], False),
        (20, ["情報提供のみ"], True),
    ],
)
def test_common_eligibility_history_and_required_jpx_flags(span, flags, expected) -> None:
    row = _analysis("1111")
    row.update(market_cap_oku=50, price_history_span_days=span, jpx_flags=flags)
    context = _discover([row]).contexts["1111"]
    assert context.common_eligible is expected
    assert bool(context.reasons) is expected
    if not expected:
        assert context.normalized_per_3fy_sector_gap is None


@pytest.mark.parametrize(
    ("cap", "expected"),
    [
        (0.001, True),
        (99.999, True),
        (100.0, True),
        (None, False),
        (0.0, False),
        (-1.0, False),
        (float("nan"), False),
        (float("inf"), False),
    ],
)
def test_common_eligibility_marketcap_boundaries(cap, expected) -> None:
    row = _analysis("1111")
    row["market_cap_oku"] = cap
    assert _discover([row]).contexts["1111"].common_eligible is expected


@pytest.mark.parametrize("sector", ["銀行業", "保険業", "その他金融業", "証券・商品先物取引業"])
def test_asset_excludes_finance_and_requires_asset_proxy(sector: str) -> None:
    finance = _analysis("1111", asset_ratio=9.0)
    finance["sector_33"] = sector
    net_cash = _analysis("2222", asset_ratio=0.0)
    net_cash["metrics"]["net_cash_to_market_cap"] = 9.0
    native = _analysis("3333", asset_ratio=0.5)
    assert _ranks(_discover([finance, net_cash, native]), "asset-value") == {"3333": 1}


def test_asset_tiebreak_uses_pbr_before_net_cash_context() -> None:
    cheaper = _analysis("1111")
    cheaper["metrics"].update(pbr_sector_gap=-0.5, net_cash_to_market_cap=0.01)
    higher_cash = _analysis("2222")
    higher_cash["metrics"].update(pbr_sector_gap=-0.1, net_cash_to_market_cap=10.0)
    assert _ranks(_discover([higher_cash, cheaper]), "asset-value") == {"1111": 1, "2222": 2}


@pytest.mark.parametrize("fcf", [-1e30, -0.01, 0.0, 0.08, 1e30])
def test_reinvestment_signed_fcf_is_last_order_key(fcf) -> None:
    cheaper = _analysis("1111")
    expensive = _analysis("2222")
    cheaper["metrics"].update(fcf_yield=fcf, p_s_sector_gap=-0.5)
    expensive["metrics"].update(fcf_yield=1e30, p_s_sector_gap=-0.4)
    assert _ranks(_discover([expensive, cheaper]), "reinvestment-value") == {"1111": 1, "2222": 2}
    assert reinvestment_coordinates(cheaper)["fcf_yield"] == fcf


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("fcf_yield", None),
        ("fcf_yield", float("nan")),
        ("fcf_yield", float("inf")),
        ("fcf_yield", -float("inf")),
        ("sales_yoy", 0.0),
        ("sales_yoy", -0.01),
        ("sales_yoy", None),
        ("p_s_sector_gap", 0.0),
        ("same_state_equity_yen", 0.0),
    ],
)
def test_reinvestment_missing_and_nonpositive_inputs_do_not_select(field, value) -> None:
    row = _analysis("1111")
    row["metrics"][field] = value
    assert _ranks(_discover([row]), "reinvestment-value") == {}
    assert _discover([row]).contexts["1111"].common_eligible


@pytest.mark.parametrize(
    ("changed", "margin", "capital_return"),
    [
        ({"operating_profit_ttm": -12.0}, -0.12, -12 / 110),
        ({"operating_profit_ttm": 0.0}, 0.0, 0.0),
        ({"operating_profit_ttm": None}, None, None),
        ({"sales_ttm": None}, None, 12 / 110),
        ({"sales_ttm": 0.0}, None, 12 / 110),
        ({"sales_ttm": -100.0}, None, 12 / 110),
        ({"cash": None}, 0.12, None),
        ({"cash": 140.0}, 0.12, None),
        ({"cash": 150.0}, 0.12, None),
    ],
)
def test_reinvestment_display_preserves_signed_values_and_valid_denominators(
    changed, margin, capital_return
) -> None:
    row = _analysis("1111")
    row["metrics"].update(changed)
    coordinates = reinvestment_coordinates(row)
    assert coordinates["operating_margin"] == (
        pytest.approx(margin) if margin is not None else None
    )
    assert coordinates["operating_return_on_capital_proxy"] == (
        pytest.approx(capital_return) if capital_return is not None else None
    )
    assert coordinates["sales_yoy"] == 0.05
    assert _ranks(_discover([row]), "reinvestment-value") == {}


def test_sector_boundary_and_market_fallback_are_inclusive() -> None:
    low = [
        {**_analysis(str(1000 + i), normalized_per=100.0 + i), "sector_33": "thin"}
        for i in range(10)
    ]
    high = [
        {**_analysis(str(2000 + i), normalized_per=10.0 + i), "sector_33": "thick"}
        for i in range(10)
    ]
    for row in high:
        row["metrics"]["operating_profit_ttm"] = 100.0
    full = _discover(low + high)
    thin = _discover(low[:-1] + high)
    assert set(_ranks(full, "reinvestment-value")) == {r["ticker"] for r in low + high}
    assert set(_ranks(thin, "reinvestment-value")) == {r["ticker"] for r in high}
    assert full.contexts["1000"].normalized_per_3fy_sector_gap == pytest.approx(100 / 104.5 - 1)
    assert thin.contexts["1000"].normalized_per_3fy_sector_gap == pytest.approx(100 / 19 - 1)
    assert thin.contexts["2000"].normalized_per_3fy_sector_gap == pytest.approx(10 / 14.5 - 1)


def test_normalized_gap_and_quality_floor_ignore_common_ineligible_extreme_values() -> None:
    rows = [_analysis(str(1000 + i), normalized_per=10.0 + i) for i in range(10)]
    for row in rows:
        row["metrics"]["normalized_per_3fy_sector_gap"] = -999.0
    baseline = _discover(rows)
    extreme = _analysis("9001", normalized_per=0.001)
    extreme["market_cap_oku"] = None
    extreme["metrics"]["operating_profit_ttm"] = 1e30
    augmented = _discover([*rows, extreme])
    assert {t: augmented.contexts[t] for t in baseline.contexts} == baseline.contexts
    assert baseline.contexts["1000"].normalized_per_3fy_sector_gap == pytest.approx(10 / 14.5 - 1)


def test_null_current_sort_and_current_per_fallback() -> None:
    a = _analysis("1111")
    b = _analysis("2222")
    a["metrics"]["fcf_yield"] = None
    assert _ranks(_discover([a, b]), "current-earnings-power") == {"2222": 1, "1111": 2}
    b["per_forward"] = 0.0
    b["per_trailing"] = 1.0
    b["metrics"]["per_trailing_sector_gap"] = -0.99
    assert _ranks(_discover([a, b]), "current-earnings-power")["2222"] == 1


def test_no_computable_approach_and_empty_run_are_completed_results() -> None:
    row = _analysis("1111")
    row["per_forward"] = None
    row["per_trailing"] = -1.0
    row["metrics"].update(normalized_per_3fy=None, asset_backed_ratio=None, sales_yoy=0.0)
    result = _discover([row])
    assert result.candidate_count == 0
    assert result.contexts["1111"].common_eligible
    empty = _discover([])
    assert empty.contexts == {}
    assert empty.candidate_count == 0
    assert empty.candidate_counts_by_approach == dict.fromkeys(APPROACH_IDS, 0)


def test_duplicate_tickers_rejected_and_hash_still_binds_policy(monkeypatch) -> None:
    with pytest.raises(CandidateDiscoveryContractError, match="duplicate"):
        _discover([_analysis("1111"), _analysis("1111")])
    baseline = candidate_discovery_method_hash(RULES, required_jpx_flags=REQUIRED_JPX_FLAGS)
    assert baseline == candidate_discovery_method_hash(
        RULES, required_jpx_flags=tuple(reversed(REQUIRED_JPX_FLAGS))
    )
    assert baseline != candidate_discovery_method_hash(RULES, required_jpx_flags=())
    monkeypatch.setattr(candidates_module, "UNIVERSE_SCOPE_REVISION", "twenty-bar-v1")
    assert baseline != candidate_discovery_method_hash(RULES, required_jpx_flags=REQUIRED_JPX_FLAGS)


@pytest.mark.parametrize(
    "mutation",
    [
        {"reasons": [{"valuation_approach_id": "unknown", "valuation_method_id": "m", "rank": 1}]},
        {
            "reasons": [
                {"valuation_approach_id": "asset-value", "valuation_method_id": "m", "rank": 0}
            ]
        },
        {
            "reasons": [
                {"valuation_approach_id": "asset-value", "valuation_method_id": "m", "rank": True}
            ]
        },
        {"normalized_per_3fy_sector_gap": float("nan")},
        {"common_eligible": "true"},
        {"common_eligible": False},
        {"unknown": 0},
    ],
)
def test_context_strict_shape(mutation) -> None:
    payload = _discover([_analysis("1111")]).contexts["1111"].model_dump(mode="json")
    payload.update(mutation)
    with pytest.raises(
        ValueError,
        match=r"StockCandidateContext|candidate",
    ):
        StockCandidateContext.model_validate(payload)


def test_context_rejects_duplicate_and_out_of_order_reasons_and_zero_is_known() -> None:
    payload = _discover([_analysis("1111")]).contexts["1111"].model_dump(mode="json")
    assert StockCandidateContext.model_validate(payload).normalized_per_3fy_sector_gap == 0.0
    for reasons in (list(reversed(payload["reasons"])), payload["reasons"][:1] * 2):
        with pytest.raises(
            ValueError,
            match=r"StockCandidateContext|candidate",
        ):
            StockCandidateContext.model_validate({**payload, "reasons": reasons})


@pytest.mark.parametrize("mutation", ["rank_gap", "depth", "counts"])
def test_stored_run_validator_rejects_inconsistent_union(mutation) -> None:
    result = _discover([_analysis("1111"), _analysis("2222")])
    header = {
        "schema_version": 1,
        "kind": "stock_analysis_run",
        "candidate_discovery": result.summary_payload(),
    }
    rows = [
        {"ticker": t, "candidate": c.model_dump(mode="json")} for t, c in result.contexts.items()
    ]
    if mutation == "rank_gap":
        rows[0]["candidate"]["reasons"][0]["rank"] = 3
    elif mutation == "depth":
        header["candidate_discovery"]["method"]["nomination_depth"] = 1
    else:
        header["candidate_discovery"]["diagnostics"]["candidate_count"] = 0
    with pytest.raises(
        ValueError,
        match=r"ranks|depth|diagnostics",
    ):
        validate_candidate_run(header, rows)


def test_four_independent_top20_sets_form_exact_80_stock_union() -> None:
    rows = []
    for group in range(4):
        for index in range(21):
            row = _analysis(str(1000 + group * 100 + index))
            row["per_forward"] = row["per_trailing"] = None
            row["metrics"].update(normalized_per_3fy=None, asset_backed_ratio=None, sales_yoy=None)
            if group == 0:
                row["per_forward"] = 10.0
            elif group == 1:
                row["metrics"]["normalized_per_3fy"] = 10.0
            elif group == 2:
                row["metrics"]["asset_backed_ratio"] = 0.5
            else:
                row["metrics"]["sales_yoy"] = 0.05
            rows.append(row)
    result = _discover(rows)
    assert result.candidate_count == 80
    assert result.candidate_counts_by_approach == dict.fromkeys(APPROACH_IDS, 20)
    for group, approach in enumerate(APPROACH_IDS):
        assert _ranks(result, approach) == {
            str(1000 + group * 100 + index): index + 1 for index in range(20)
        }
        assert result.contexts[str(1020 + group * 100)].reasons == ()


@pytest.mark.parametrize("gap", [None, float("nan")])
def test_current_earnings_requires_the_gap_of_the_chosen_native_multiple(gap):
    row = _analysis("1111")
    row["metrics"]["per_forward_sector_gap"] = gap
    row["metrics"]["chosen_per_sector_gap"] = -0.9
    row["metrics"]["per_trailing_sector_gap"] = -0.8
    assert _ranks(_discover([row]), "current-earnings-power") == {}
