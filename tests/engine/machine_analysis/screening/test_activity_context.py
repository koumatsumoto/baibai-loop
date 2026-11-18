"""売買代金の円規模と倍率、測れなかった倍率と観測したゼロを区別する。"""

from copy import deepcopy

import pytest
from tests.helpers.technical_discovery import _metadata, _models

from baibai_engine.machine_analysis.screening.policies.discovery import project_discovery_row
from baibai_engine.machine_analysis.screening.policies.technical_discovery import (
    evaluate_technical_strategy,
)

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


def test_same_relative_expansion_at_different_yen_scale_does_not_rank_the_company():
    stock, meta = _models(), _metadata()
    price = stock[1].payload["price"]
    price[FIELD] = deepcopy(ACTIVITY)
    small = project_discovery_row("1234", models=stock, metadata=meta)
    for key in ("current_yen", "prior_mean_yen", "prior_median_yen", "prior_min_yen"):
        price[FIELD][key] *= 100
    large = project_discovery_row("1234", models=stock, metadata=meta)
    assert small == large
    assert price[FIELD]["current_yen"] == 40000000
    technical = evaluate_technical_strategy("1234", models=stock, metadata=meta)
    assert technical["observations"][FIELD]["value"] == 4.0
    assert technical["routes"]["turnover_expansion"]["state"] == "matched"


@pytest.mark.parametrize(
    ("change", "value", "reason"),
    [
        ({"current_yen": 0, "current_to_prior_mean_ratio": 0.0}, 0.0, None),
        (INCOMPLETE_ACTIVITY, None, "recorded_null"),
        ({"current_date": "2026-11-12"}, 4.0, "price_date_mismatch_or_unknown"),
    ],
)
def test_ratio_keeps_clock_and_coverage_before_it_can_be_used(change, value, reason):
    stock = _models()
    activity = {**ACTIVITY, **change}
    stock[1].payload["price"][FIELD] = activity
    answer = evaluate_technical_strategy("1234", models=stock, metadata=_metadata())
    obs = answer["observations"][FIELD]
    assert (obs["value"], obs["unavailable_reason"]) == (value, reason)
    assert obs["source_unavailable_reason"] == activity["unavailable_reason"]
    assert obs["window"]["current_date"] == activity["current_date"]
    assert obs["window"]["prior_valid_days"] == activity["prior_valid_days"]
    assert answer["routes"]["turnover_expansion"]["state"] == (
        "unavailable" if reason else "not_matched"
    )
