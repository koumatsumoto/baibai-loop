from __future__ import annotations

from math import ceil

import pytest
from tests.helpers.calibration_store import forward_row, panel_row

from baibai_engine.machine_analysis.historical_evaluation.context.builder import (
    CalibrationContextError,
    _cohort_entries,
    _horizon_context,
    _prediction_boundaries,
)
from baibai_engine.machine_analysis.historical_evaluation.context.contracts import (
    ErCalibrationHorizon,
)
from baibai_engine.machine_analysis.historical_evaluation.context.query import candidate_er_bands


def _cohorts(values_by_asof):
    panels, forwards = {}, {}
    for asof, values in values_by_asof.items():
        panels[asof] = [
            panel_row(asof, str(1000 + i), er_annual=value) for i, value in enumerate(values)
        ]
        forwards[asof] = [
            forward_row(
                asof,
                str(1000 + i),
                "3y",
                price_return=i / 100,
                total_return=i / 100 + 0.05,
                total_return_status="resolved",
                realized_dividend_sum=50.0,
                realized_dividend_fy_count=3,
            )
            for i in range(len(values))
        ]
    return panels, forwards


def _rank(values, probability):
    ordered = sorted(values)
    return ordered[ceil(len(ordered) * probability) - 1]


def test_all_fixed_band_members_match_lookup_ranges_and_distribution():
    values = [i / 1000 + 0.00000049 for i in range(100)]
    values[17:19] = [0.019, 0.019]
    panels, forwards = _cohorts(
        {"2020-01-31": values, "2020-02-28": [i / 1000 + 0.01000049 for i in range(100)]}
    )
    asofs = list(panels)
    horizon = ErCalibrationHorizon.model_validate(
        _horizon_context(
            "3y",
            asofs,
            panels=panels,
            forwards=forwards,
            fixed_boundaries=_prediction_boundaries(panels, asofs),
        )
    )
    assert horizon.bands[0].upper_er_annual == 0.019
    assert candidate_er_bands(horizon, 0.019)[0].quintile == 1
    assert candidate_er_bands(horizon, 0.01900049)[0].quintile == 2
    assert candidate_er_bands(horizon, 0.085)[-1].band_id == "er_gte_8_5pct"
    entries = {
        asof: _cohort_entries(panels[asof], forwards[asof], horizon="3y", years=3) for asof in asofs
    }
    for band in horizon.bands:
        per_cohort = []
        for rows in entries.values():
            members = [
                row
                for row in rows
                if (
                    band.lower_er_annual is None
                    or (
                        row.er_annual >= band.lower_er_annual
                        if band.quintile is None
                        else row.er_annual > band.lower_er_annual
                    )
                )
                and (band.upper_er_annual is None or row.er_annual <= band.upper_er_annual)
            ]
            assert members
            assert all(band in candidate_er_bands(horizon, row.er_annual) for row in members)
            per_cohort.append(members)
        pooled = [row for rows in per_cohort for row in rows]
        assert band.median_n == _rank([len(rows) for rows in per_cohort], 0.5)
        assert band.median_predicted_er_annual == round(
            _rank([row.er_annual for row in pooled], 0.5), 6
        )
        for basis, value, trap in (
            (band.bases[0], "total_annual", "total_trap"),
            (band.bases[1], "price_annual", "price_trap"),
        ):
            observed = [getattr(row, value) for row in pooled]
            assert basis.ticker_equal.n == len(pooled)
            for field, probability in (("median", 0.5), ("q25", 0.25), ("q10", 0.1)):
                assert getattr(basis.ticker_equal, field) == round(_rank(observed, probability), 6)
            assert basis.ticker_equal.trap_rate == round(
                sum(getattr(row, trap) for row in pooled) / len(pooled), 6
            )
            assert basis.cohort_equal.median == round(
                _rank(
                    [_rank([getattr(row, value) for row in rows], 0.5) for rows in per_cohort], 0.5
                ),
                6,
            )


def test_separated_cohorts_do_not_fabricate_statistics_for_empty_fixed_bands():
    panels, forwards = _cohorts(
        {
            "2020-01-31": [(i // 10) / 100 for i in range(100)],
            "2020-02-28": [(100 + i // 10) / 100 for i in range(100)],
        }
    )
    horizon = ErCalibrationHorizon.model_validate(
        _horizon_context(
            "3y",
            list(panels),
            panels=panels,
            forwards=forwards,
            fixed_boundaries=_prediction_boundaries(panels, list(panels)),
        )
    )
    assert horizon.cohort_count == 2
    for band in horizon.bands[:4]:
        assert band.median_n == 0
        for basis in band.bases:
            assert basis.observed_cohort_count == 1
            assert basis.coverage.missing_n == 0
            assert basis.ticker_equal is not None
            assert basis.ticker_equal.n == basis.coverage.declared_n


@pytest.mark.parametrize(
    "values",
    [[0.1] * 100, [0.1 + i * 0.000000001 for i in range(100)]],
    ids=["identical-values", "rounding-collapses-boundaries"],
)
def test_duplicate_serialized_boundaries_are_rejected(values):
    panels, _forwards = _cohorts({"2020-01-31": values})
    with pytest.raises(CalibrationContextError, match="boundaries are not increasing"):
        _prediction_boundaries(panels, list(panels))
