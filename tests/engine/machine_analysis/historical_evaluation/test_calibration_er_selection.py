import pytest
from tests.helpers.calibration_store import forward_row, panel_row

from baibai_engine.machine_analysis.historical_evaluation.evaluation.cohorts import evaluate_cohorts
from baibai_engine.machine_analysis.historical_evaluation.legacy_screening.rule_config import (
    load_screening_rules,
)


@pytest.mark.parametrize("missing_top", [True, False])
def test_pure_er_top_five_is_selected_before_future_outcome_availability(missing_top):
    asof = "2020-01-31"
    panel = [
        panel_row(asof, f"{1000 + index}", er_annual=1.0 - index / 200) for index in range(120)
    ]
    # The sixth-ranked security's large return must not replace the missing first.
    forwards = [
        forward_row(
            asof,
            row.ticker,
            "3y",
            price_return=None if index == 0 and missing_top else 10.0 if index == 5 else 0.0,
            status="unresolved_missing_exit" if index == 0 and missing_top else "resolved",
        )
        for index, row in enumerate(panel)
    ]
    results = evaluate_cohorts(
        {asof: panel},
        {asof: forwards},
        horizons=["3y"],
        candidate_discovery_rules=load_screening_rules().candidate_discovery,
    )
    group = results["3y"]["cohorts"][0]["candidate_discovery"]["pure_er_top5"]
    assert group["n"] == (4 if missing_top else 5)
    assert group["mean_excess"] == 0.0
