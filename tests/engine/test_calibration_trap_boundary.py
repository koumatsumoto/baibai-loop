from __future__ import annotations

from types import SimpleNamespace

import pytest

from baibai_engine.machine_analysis.screening.calibration.evaluation.axes import _evaluate_axis
from baibai_engine.machine_analysis.screening.calibration.evaluation.policy import (
    MIN_AXIS_SAMPLE,
    AxisSpec,
)
from baibai_engine.machine_analysis.screening.calibration.evaluation.statistics import _group_stats


@pytest.mark.parametrize(("excess", "trap_rate"), [(-0.21, 1.0), (-0.20, 1.0), (-0.19, 0.0)])
def test_twenty_point_underperformance_boundary_is_shared(excess: float, trap_rate: float) -> None:
    assert _group_stats([excess])["trap_rate"] == trap_rate
    # Axis statistics consume only the named coordinate and ticker from each row.
    rows = [
        SimpleNamespace(ticker=f"{index:04}", per_trailing=float(index + 1))
        for index in range(MIN_AXIS_SAMPLE)
    ]
    result = _evaluate_axis(
        AxisSpec(name="per_trailing", direction=-1),
        rows,
        {row.ticker: excess for row in rows},
    )
    assert result is not None
    assert result["best_decile_trap_rate"] == trap_rate
    assert all(group["trap_rate"] == trap_rate for group in result["deciles"])
