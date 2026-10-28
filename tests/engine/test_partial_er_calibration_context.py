from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict, replace
from datetime import date, datetime
from zoneinfo import ZoneInfo

import pytest
from tests.helpers.calibration_store import panel_row

from baibai_engine.machine_analysis.historical_evaluation.context.contracts import (
    ErCalibrationContextArtifact,
)
from baibai_engine.machine_analysis.historical_evaluation.context.query import (
    candidate_er_band_context,
)
from baibai_engine.machine_analysis.historical_evaluation.context import (
    CalibrationContextError,
    build_er_distribution_context,
)
from baibai_engine.machine_analysis.historical_evaluation.forward import ForwardReturnRow
from baibai_engine.machine_analysis.historical_evaluation.horizons import require_horizon


def inputs(dates=("2020-01-31",), *, complete=True):
    panels = {
        asof: [
            panel_row(asof, f"S{index:02d}", in_population=True, er_annual=index / 100)
            for index in range(10)
        ]
        for asof in dates
    }
    forwards = {
        asof: [
            ForwardReturnRow(
                asof=asof,
                ticker=row.ticker,
                horizon=horizon,
                target_date=require_horizon(horizon)
                .target_date(date.fromisoformat(asof))
                .isoformat(),
                resolved=True,
                price_return=index / 10 - 0.5,
                stale_price=False,
                entry_date=asof,
                exit_date=require_horizon(horizon)
                .target_date(date.fromisoformat(asof))
                .isoformat(),
                adjustment_factor_coverage="complete",
                realized_dividend_sum=3.0,
                realized_dividend_fy_count=3,
                total_return=index / 10 - 0.47,
                total_return_status="resolved",
            )
            for horizon in ("3y", "5y")
            for index, row in enumerate(rows)
        ]
        for asof, rows in panels.items()
    }
    evaluation = {
        "screening_rules_hash": "synthetic-rules",
        "er_model_version": "expected-return-v1",
        "scope": {
            "run_purpose": "empirical_change_evidence",
            "decision_subject": "estimator_policy",
            "required_metrics": ["er_level_calibration"],
            "mandatory_metrics": ["er_calibration"],
            "required_asofs": list(dates),
        },
        "evidence_readiness": {
            "evidence_complete": complete,
            "evidence_status": "eligible" if complete else "blocked",
            "blocking_reasons": [] if complete else ["entry_price_gap"],
        },
        "cohort_integrity": [
            {
                "asof": asof,
                "horizon": horizon,
                "integrity_status": "eligible" if complete else "blocked",
                "metric_statuses": {
                    "er_calibration": "eligible",
                    "er_level_calibration": "eligible",
                },
            }
            for asof in dates
            for horizon in ("3y", "5y")
        ],
        "results": {
            horizon: {
                "cohorts": [
                    {
                        "asof": asof,
                        "coverage": {
                            "master_snapshot_status": "exact_date",
                            "survivorship_coverage_status": "complete",
                            "adjustment_factor_coverage": "complete",
                            "input_range_clamped": False,
                            "candidate_partition_complete": True,
                        },
                    }
                    for asof in dates
                ]
            }
            for horizon in ("3y", "5y")
        },
    }
    return evaluation, panels, forwards


def build(evaluation, panels, forwards):
    return build_er_distribution_context(
        evaluation,
        panels,
        forwards,
        generated_at=datetime(2026, 10, 1, tzinfo=ZoneInfo("Asia/Tokyo")),
    )


def missing_dividend(row):
    return replace(
        row,
        realized_dividend_sum=None,
        realized_dividend_fy_count=0,
        total_return=None,
        total_return_status="unresolved_missing_dividend",
    )


def missing_price(row):
    return replace(
        missing_dividend(row),
        resolved=False,
        price_return=None,
        entry_date=None,
        exit_date=None,
        status="unresolved_missing_entry",
        total_return_status="unresolved_price_return",
    )


def test_outcome_missingness_does_not_select_predictions_or_remove_price():
    evaluation, panels, forwards = inputs()
    complete = build(evaluation, panels, forwards)
    evaluation["evidence_readiness"] = {
        "evidence_status": "blocked",
        "evidence_complete": False,
        "blocking_reasons": ["entry_price_gap"],
    }
    forwards["2020-01-31"] = [
        missing_dividend(row) if row.ticker in {"S08", "S09"} else row
        for row in forwards["2020-01-31"]
    ]
    partial = build(evaluation, panels, forwards)
    for before, after in zip(complete["horizons"], partial["horizons"], strict=True):
        assert [band["upper_er_annual"] for band in after["bands"][:4]] == [0.01, 0.03, 0.05, 0.07]
        assert [band["upper_er_annual"] for band in before["bands"]] == [
            band["upper_er_annual"] for band in after["bands"]
        ]
        total, price = after["bands"][4]["bases"]
        assert total["ticker_equal"] is None
        assert total["coverage"] == {
            "declared_n": 2,
            "observed_n": 0,
            "missing_n": 2,
            "missing_reason_counts": {"unresolved_missing_dividend": 2},
        }
        assert price["ticker_equal"] == before["bands"][4]["bases"][1]["ticker_equal"]
        assert price["coverage"]["observed_n"] == 2
    assert partial["evidence_complete"] is False


@pytest.mark.parametrize("transform", [missing_dividend, missing_price])
def test_all_missing_basis_retains_declared_population_and_null_statistics(transform):
    evaluation, panels, forwards = inputs(complete=False)
    forwards["2020-01-31"] = [transform(row) for row in forwards["2020-01-31"]]
    raw = build(evaluation, panels, forwards)
    artifact = ErCalibrationContextArtifact.model_validate(raw)
    for horizon in artifact.horizons:
        for band in horizon.bands:
            total = band.bases[0]
            assert total.coverage.declared_n == total.coverage.missing_n
            assert total.ticker_equal is None
            assert total.cohort_equal is None
            assert total.observed_cohort_count == 0
    summary = candidate_er_band_context(artifact, 0.09)[0]
    assert summary["bands"][0]["bases"][0]["ticker_equal"] is None
    assert summary["bands"][0]["bases"][0]["coverage"]["missing_n"] == 2


def test_one_missing_name_keeps_all_declared_months_and_coverage():
    evaluation, panels, forwards = inputs(("2020-01-31", "2020-02-28"), complete=False)
    forwards["2020-02-28"] = [
        missing_price(row) if row.ticker == "S09" else row for row in forwards["2020-02-28"]
    ]
    raw = build(evaluation, panels, forwards)
    assert raw["required_asofs"] == ["2020-01-31", "2020-02-28"]
    for horizon in raw["horizons"]:
        assert horizon["cohort_count"] == 2
        total = horizon["bands"][4]["bases"][0]
        assert total["coverage"]["declared_n"] == 4
        assert total["coverage"]["observed_n"] == 3
        assert total["coverage"]["missing_reason_counts"] == {"unresolved_missing_entry": 1}
        assert total["observed_cohort_count"] == 2


@pytest.mark.parametrize("fault", ["panel", "forward", "input", "value", "model", "integrity"])
def test_corruption_and_missing_input_are_not_partial(fault):
    evaluation, panels, forwards = inputs(complete=False)
    if fault == "panel":
        panels.clear()
    elif fault == "forward":
        forwards["2020-01-31"].pop()
    elif fault == "input":
        evaluation["results"]["3y"]["cohorts"][0]["coverage"]["candidate_partition_complete"] = (
            False
        )
    elif fault == "value":
        forwards["2020-01-31"][0] = replace(forwards["2020-01-31"][0], price_return=float("nan"))
    elif fault == "model":
        evaluation["er_model_version"] = "different-model"
    else:
        evaluation["cohort_integrity"].clear()
    with pytest.raises(CalibrationContextError):
        build(evaluation, panels, forwards)


def test_corrupt_coverage_cannot_relabel_missing_as_observed():
    from pydantic import ValidationError

    evaluation, panels, forwards = inputs()
    raw = deepcopy(build(evaluation, panels, forwards))
    raw["horizons"][0]["bands"][0]["bases"][0]["coverage"]["observed_n"] -= 1
    with pytest.raises(ValidationError):
        ErCalibrationContextArtifact.model_validate(raw)


@pytest.mark.parametrize(
    "value",
    [float("nan"), float("inf"), float("-inf")],
    ids=["nan", "positive-infinity", "negative-infinity"],
)
def test_unresolved_nonfinite_price_is_corrupt_not_normal_missing(value):
    from baibai_engine.machine_analysis.historical_evaluation.store import forward_row_from_mapping

    evaluation, panels, forwards = inputs(complete=False)
    row = replace(missing_price(forwards["2020-01-31"][0]), price_return=value)
    with pytest.raises(ValueError, match="price return must be finite"):
        forward_row_from_mapping(asdict(row))
    forwards["2020-01-31"][0] = row
    with pytest.raises(CalibrationContextError, match="forward observation is corrupt"):
        build(evaluation, panels, forwards)


def test_candidate_context_keeps_both_basis_coverage_and_the_limit(tmp_path):
    import yaml

    from baibai_engine.machine_analysis.historical_evaluation.context.query import (
        candidate_er_band_context,
        load_er_calibration_context,
    )

    evaluation, panels, forwards = inputs(complete=False)
    forwards["2020-01-31"] = [
        missing_dividend(row) if row.ticker in {"S08", "S09"} else row
        for row in forwards["2020-01-31"]
    ]
    path = tmp_path / "context.yaml"
    path.write_text(yaml.safe_dump(build(evaluation, panels, forwards)))
    loaded = load_er_calibration_context(
        path,
        expected_rules_hash="synthetic-rules",
        expected_er_model_version="expected-return-v1",
        as_of=date(2026, 10, 1),
    )
    assert loaded.artifact.evidence_complete is False
    bands = candidate_er_band_context(loaded.artifact, 0.09)[0]["bands"]
    total, price = bands[0]["bases"]
    assert total["ticker_equal"] is None
    assert total["coverage"]["missing_reason_counts"] == {"unresolved_missing_dividend": 2}
    assert price["ticker_equal"]["n"] == 2
