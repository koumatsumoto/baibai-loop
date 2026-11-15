"""商い拡大の相対条件と円規模・観測不足を同じ候補で読む。"""

from copy import deepcopy
from dataclasses import asdict, replace

import pytest
from tests.engine.machine_analysis.screening.test_financial_input_review import (
    case,
    project_unchanged,
)
from tests.engine.machine_analysis.screening.test_input_review_attention import result

from baibai_engine.investment_judgment.workflow.screening_brief import (
    technical_financial_screening_brief,
)
from baibai_engine.machine_analysis.screening.policies.technical_financial import (
    discovery_display_order,
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


def payloads(activity):
    data = case(reason=None)
    data["market_context"].setdefault("price", {})[FIELD] = deepcopy(activity)
    return data


@pytest.mark.parametrize("state", ["available", "incomplete", "zero", "old_date", "null", "empty"])
def test_original_activity_values_window_and_absence_reach_first_card(state):
    activity = deepcopy(ACTIVITY)
    if state == "incomplete":
        activity.update(
            status="unavailable",
            unavailable_reason="incomplete_turnover_window",
            prior_valid_days=9,
            prior_mean_yen=None,
            prior_median_yen=None,
            prior_min_yen=None,
            current_to_prior_mean_ratio=None,
        )
    elif state == "zero":
        activity.update(current_yen=0.0, current_to_prior_mean_ratio=0.0)
    elif state == "old_date":
        activity["current_date"] = "2026-11-12"
    elif state == "null":
        activity = None
    elif state == "empty":
        activity = {}
    data = payloads(activity)
    row = project_unchanged(data)
    card = technical_financial_screening_brief(result((row,)), lane=row.screening_lane)["cards"][0]
    assert row.technical[FIELD] == activity
    assert card["technical"][FIELD] == activity
    if state == "incomplete":
        assert card["technical"][FIELD]["current_yen"] == 400000.0
        assert card["technical"]["routes"]["turnover_expansion"]["state"] == "unavailable"
    elif state == "old_date":
        assert card["technical"]["routes"]["turnover_expansion"]["state"] == "unavailable"
    if activity:
        card["technical"][FIELD]["current_yen"] = 999.0
        assert row.technical[FIELD]["current_yen"] == activity["current_yen"]
        row.technical[FIELD]["current_yen"] = 111.0
        assert data["market_context"]["price"][FIELD] == activity


@pytest.mark.parametrize("missing_model", [False, True])
def test_unrecorded_activity_is_not_filled_or_turned_into_zero(missing_model):
    data = payloads(ACTIVITY)
    baseline = project_unchanged(data)
    if missing_model:
        del data["market_context"]
    else:
        del data["market_context"]["price"][FIELD]
    row = project_unchanged(data)
    assert row.financial_gate == baseline.financial_gate
    assert FIELD not in row.technical
    brief = technical_financial_screening_brief(result((row,)), comparison_tickers=("1234",))
    assert all(FIELD not in card["technical"] for card in brief["cards"])
    detail = brief["comparison_rows"][0]
    assert FIELD not in detail["technical"]
    assert detail["technical"]["observation_failures"][FIELD]["source_state"] == (
        "model_not_generated" if missing_model else "not_recorded"
    )


def test_same_ratio_at_different_yen_scale_does_not_change_T_G_F_or_attention():
    small = project_unchanged(payloads(ACTIVITY))
    large_activity = deepcopy(ACTIVITY)
    for key in ("current_yen", "prior_mean_yen", "prior_median_yen", "prior_min_yen"):
        large_activity[key] *= 100
    large = project_unchanged(payloads(large_activity))
    assert small.technical[FIELD] != large.technical[FIELD]
    left, right = asdict(small), asdict(large)
    del left["technical"][FIELD], right["technical"][FIELD]
    assert left == right
    assert small.technical["routes"]["turnover_expansion"]["state"] == "matched"
    companion = replace(small, ticker="2345")
    assert discovery_display_order((small, companion)) == discovery_display_order(
        (large, companion)
    )


def test_unknown_activity_fields_are_preserved_without_new_inference():
    activity = {**ACTIVITY, "source_detail": {"late_receipt": True, "clock": None}}
    row = project_unchanged(payloads(activity))
    assert row.technical[FIELD] == activity
    assert "adv_yen" not in row.technical[FIELD]
    assert "quantity_limit" not in row.technical[FIELD]
