"""売買代金の円規模と倍率、測れなかった倍率と観測したゼロを区別する。"""

from copy import deepcopy

import pytest
from tests.engine.machine_analysis.screening.test_technical_strategy import _strategy_models
from tests.helpers.technical_discovery import _metadata
from tests.helpers.technical_financial import _no_match

from baibai_engine.machine_analysis.screening.policies.discovery import project_discovery_row

FIELD = "turnover_activity_20d"
ACTIVITY = {
    "basis": "traded_value_yen_not_share_volume",
    "window_basis": "prior_20_common_trading_sessions_excluding_current",
    "current_date": "2026-11-13",
    "prior_start_date": "2026-10-15",
    "prior_end_date": "2026-11-12",
    "current_yen": 400000.0,
    "prior_mean_yen": 100000.0,
    "prior_median_yen": 75000.0,
    "prior_min_yen": 0.0,
    "prior_valid_days": 20,
    "current_to_prior_mean_ratio": 4.0,
    "status": "available",
    "unavailable_reason": None,
}
INCOMPLETE_ACTIVITY = {
    **ACTIVITY,
    "status": "unavailable",
    "unavailable_reason": "incomplete_turnover_window",
    "current_yen": 0,
    "prior_valid_days": 9,
    "prior_mean_yen": None,
    "prior_median_yen": None,
    "prior_min_yen": None,
    "current_to_prior_mean_ratio": None,
}


def no_other_match():
    stock = _strategy_models()
    _no_match(stock)
    market = stock[1].payload
    market["relative"]["market_conditioned_60d"]["benchmarks"]["1321"]["down"][
        "own_mean_return"
    ] = -0.1
    market["price"].update(
        close=110.0, resolved_date="2026-11-13", price_basis="price_adjusted_close"
    )
    return stock


def test_same_relative_expansion_at_different_yen_scale_does_not_rank_the_company():
    stock, meta = no_other_match(), _metadata()
    price = stock[1].payload["price"]
    price[FIELD] = deepcopy(ACTIVITY)
    small = project_discovery_row("1234", models=stock, metadata=meta)
    for key in ("current_yen", "prior_mean_yen", "prior_median_yen", "prior_min_yen"):
        price[FIELD][key] *= 100
    large = project_discovery_row("1234", models=stock, metadata=meta)
    assert large == small
    assert large["technical_state"] == "matched"
    assert large["price_ready"]


@pytest.mark.parametrize(
    ("change", "state"),
    [
        ({"current_yen": 0, "current_to_prior_mean_ratio": 0.0}, "not_matched"),
        (INCOMPLETE_ACTIVITY, "unavailable"),
        ({"current_date": "2026-11-12"}, "unavailable"),
    ],
)
def test_missing_or_outdated_turnover_is_distinct_from_observed_zero(change, state):
    stock = no_other_match()
    stock[1].payload["price"][FIELD] = {**ACTIVITY, **change}
    row = project_discovery_row("1234", models=stock, metadata=_metadata())
    assert row["technical_state"] == state
    assert row["price_ready"]
