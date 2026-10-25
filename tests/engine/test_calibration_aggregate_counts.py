from __future__ import annotations

from types import SimpleNamespace

from baibai_engine.machine_analysis.screening.calibration.evaluation.aggregate import _aggregate
from baibai_engine.machine_analysis.screening.calibration.evaluation.axes import _evaluate_axis
from baibai_engine.machine_analysis.screening.calibration.evaluation.policy import (
    MIN_AXIS_SAMPLE,
    AxisSpec,
)


def test_defined_deciles_do_not_disappear_when_rank_correlation_is_undefined() -> None:
    rows = [
        SimpleNamespace(ticker=f"{index:04}", per_trailing=float(index + 1))
        for index in range(MIN_AXIS_SAMPLE)
    ]
    axis = _evaluate_axis(
        AxisSpec(name="per_trailing", direction=-1),
        rows,
        {row.ticker: 0.1 for row in rows},
    )
    assert axis is not None
    assert axis["rank_ic"] is None
    result = _aggregate([{"axes": {"per_trailing": axis}}])
    summary = result["axes"]["per_trailing"]
    assert summary["cohorts"] == 1
    assert summary["rank_ic_cohorts"] == 0
    assert summary["mean_rank_ic"] is None
    assert summary["mean_best_decile_median_excess"] == 0.1
