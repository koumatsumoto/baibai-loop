from __future__ import annotations

from itertools import permutations

import pytest
from pydantic import ValidationError

from baibai_engine.machine_analysis.screening.policies.two_views import (
    ScreeningCoordinates,
    TwoViewPolicy,
    select_views,
)


def coordinate(ticker: str, earnings: float | None, momentum: float | None):
    return ScreeningCoordinates(ticker=ticker, earnings_yield=earnings, price_momentum=momentum)


def selected(result, view_index):
    return tuple(row.ticker for row in result.views[view_index].companies)


def test_views_keep_different_selection_reasons_without_one_global_ranking():
    rows = [
        coordinate("1001", 0.12, None),
        coordinate("1002", 0.12, -0.50),
        coordinate("1003", 0.06, 0.50),
        coordinate("1004", 0.10, 0.0),
        coordinate("1005", 0.04, None),
        coordinate("1006", 0.03, 2.0),
    ]
    result = select_views(rows, policy=TwoViewPolicy(earnings_limit=2, combined_limit=2))
    assert selected(result, 0) == ("1001", "1002")
    assert selected(result, 1) == ("1006", "1003")
    assert result.tickers == ("1001", "1002", "1003", "1006")
    assert result.views[0].eligible_count == 6
    assert result.views[1].eligible_count == 4
    assert result.views[1].companies[1].ranking_value == pytest.approx(0.425)


def test_overlap_does_not_refill_or_award_a_second_company_slot():
    rows = [coordinate("1001", 0.20, 0.50), coordinate("1002", 0.10, 0.0)]
    result = select_views(rows, policy=TwoViewPolicy(earnings_limit=1, combined_limit=1))
    assert selected(result, 0) == selected(result, 1) == ("1001",)
    assert result.tickers == ("1001",)


def test_missing_and_observed_zero_momentum_are_not_the_same_view_input():
    rows = [coordinate("1001", 0.10, None), coordinate("1002", 0.10, 0.0)]
    result = select_views(rows, policy=TwoViewPolicy(earnings_limit=2, combined_limit=2))
    assert selected(result, 0) == ("1001", "1002")
    assert selected(result, 1) == ("1002",)
    assert rows[0].price_momentum is None


def test_losing_bad_momentum_cannot_create_membership():
    rows = [
        coordinate("1001", 0.10, -0.50),
        coordinate("1002", 0.12, None),
        coordinate("1003", 0.06, 0.50),
    ]
    policy = TwoViewPolicy(earnings_limit=1, combined_limit=1)
    before = select_views(rows, policy=policy)
    after = select_views([coordinate("1001", 0.10, None), *rows[1:]], policy=policy)
    assert "1001" not in before.tickers
    assert after.tickers == before.tickers


def test_negative_and_zero_observations_are_not_purchase_gates():
    rows = [
        coordinate("1001", -0.02, -0.50),
        coordinate("1002", 0.0, 0.0),
        coordinate("1003", None, 0.50),
    ]
    result = select_views(rows, policy=TwoViewPolicy(earnings_limit=3, combined_limit=3))
    assert selected(result, 0) == selected(result, 1) == ("1002", "1001")
    assert result.views[1].companies[1].ranking_value < 0
    assert "1003" not in result.tickers


def test_rounding_is_not_applied_before_selection():
    rows = [coordinate("1001", 0.100041, None), coordinate("1002", 0.100049, None)]
    result = select_views(rows, policy=TwoViewPolicy(earnings_limit=1, combined_limit=0))
    assert result.tickers == ("1002",)
    assert result.views[0].companies[0].ranking_value == 0.100049


def test_ties_and_input_order_are_deterministic():
    rows = [coordinate(ticker, 0.10, 0.0) for ticker in ("1003", "1001", "1002")]
    policy = TwoViewPolicy(earnings_limit=2, combined_limit=2)
    for order in permutations(rows):
        result = select_views(order, policy=policy)
        assert selected(result, 0) == selected(result, 1) == ("1001", "1002")
        assert tuple(row.rank for row in result.views[0].companies) == (1, 2)


def test_unrelated_company_does_not_change_existing_ranking_values():
    rows = [coordinate("1001", 0.10, 0.0), coordinate("1002", 0.09, 0.20)]
    policy = TwoViewPolicy(earnings_limit=3, combined_limit=3)
    before = select_views(rows, policy=policy)
    after = select_views([*rows, coordinate("1003", 0.08, 0.10)], policy=policy)
    previous = {row.ticker: row.ranking_value for row in before.views[1].companies}
    current = {row.ticker: row.ranking_value for row in after.views[1].companies}
    assert {ticker: current[ticker] for ticker in previous} == previous


def test_positive_earnings_become_more_attractive_at_lower_price_with_momentum_fixed():
    policy = TwoViewPolicy(earnings_limit=1, combined_limit=1)
    before = select_views([coordinate("1001", 0.30, -0.10)], policy=policy)
    after = select_views([coordinate("1001", 0.60, -0.10)], policy=policy)
    assert after.views[1].companies[0].ranking_value > before.views[1].companies[0].ranking_value


def test_empty_and_disabled_views_do_not_fabricate_candidates():
    policy = TwoViewPolicy(earnings_limit=0, combined_limit=0)
    assert select_views([], policy=policy).tickers == ()
    result = select_views([coordinate("1001", 0.10, 0.0)], policy=policy)
    assert result.tickers == ()
    assert result.views[0].eligible_count == result.views[1].eligible_count == 1


def test_duplicate_company_rows_are_not_votes():
    row = coordinate("1001", 0.10, 0.0)
    with pytest.raises(ValueError, match="one row per ticker"):
        select_views([row, row], policy=TwoViewPolicy(earnings_limit=1, combined_limit=1))


@pytest.mark.parametrize("invalid", [float("nan"), float("inf"), True])
def test_non_numeric_observations_are_rejected_at_the_input_boundary(invalid):
    with pytest.raises(ValidationError):
        coordinate("1001", invalid, 0.0)


def test_policy_parameters_are_explicit_and_validated():
    with pytest.raises(ValidationError):
        TwoViewPolicy(earnings_limit=True, combined_limit=1)
    with pytest.raises(ValidationError):
        TwoViewPolicy(earnings_limit=1, combined_limit=1, earnings_scale=0.0)
    with pytest.raises(ValidationError):
        TwoViewPolicy(earnings_limit=1, combined_limit=1, earnings_weight=1.1)
    policy = TwoViewPolicy(earnings_limit=2, combined_limit=3)
    assert policy.model_dump()["earnings_weight"] == 0.60
    assert policy.momentum_weight == pytest.approx(0.40)
