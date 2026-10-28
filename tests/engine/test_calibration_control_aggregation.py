"""Control aggregation counts only comparable median/trap observations."""

from baibai_engine.machine_analysis.historical_evaluation.evaluation.aggregate import (
    _control_summaries,
    _QualityControlAccumulator,
)


def test_control_counts_are_derived_from_the_values_actually_aggregated():
    values = _QualityControlAccumulator(median_deltas=[], trap_deltas=[])
    values.add(0.2, -0.1, 4)
    values.add(None, -0.4, 100)
    values.add(0.9, None, 100)
    values.add(0.0, 0.1, 6)
    assert _control_summaries({"control": values})["control"] == {
        "cohorts": 2,
        "matched_weight": 10,
        "mean_stratified_median_excess_delta": 0.1,
        "mean_stratified_trap_rate_delta": 0.0,
    }
