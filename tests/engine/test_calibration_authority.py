from __future__ import annotations

from datetime import date

from baibai_engine.screening.calibration.authority import (
    CohortIntegrity,
    EvaluationScope,
    decide_authority,
)
from baibai_engine.screening.calibration.horizons import add_months_clamped


def _scope(*, purpose: str = "production_decision") -> EvaluationScope:
    return EvaluationScope(
        run_purpose=purpose,
        requested_horizons=("3m", "6m", "1y", "3y", "5y"),
        cohort_window={"start": "2020-01-31", "end": "2020-01-31"},
        required_asofs=("2020-01-31",),
        required_metrics=("selection_rank_top5", "selection_rank_top10", "er_calibration"),
    )


def _cohort(horizon: str, status: str = "eligible") -> CohortIntegrity:
    return CohortIntegrity(
        asof="2020-01-31",
        horizon=horizon,
        integrity_status=status,
        metric_statuses={
            "selection_rank_top5": status,
            "selection_rank_top10": status,
            "er_calibration": status,
        },
    )


def test_short_favorable_evidence_cannot_authorize_production_change() -> None:
    decision = decide_authority(_scope(), (_cohort("3m"), _cohort("6m")))
    assert decision.production_change_allowed is False
    assert "missing_cohort" in decision.blocking_reasons
    # A cohort with no result has nowhere else to be named, so it is listed.
    assert decision.missing_cohorts == ("2020-01-31:3y", "2020-01-31:5y")


def test_only_complete_long_horizon_scope_is_eligible() -> None:
    decision = decide_authority(_scope(), (_cohort("3y"), _cohort("5y")))
    assert decision.production_change_allowed is True
    assert decision.evidence_status == "eligible"


def test_diagnostic_run_never_has_production_authority() -> None:
    decision = decide_authority(_scope(purpose="diagnostic"), (_cohort("3y"), _cohort("5y")))
    assert decision.production_change_allowed is False
    assert "diagnostic_run_has_no_production_authority" in decision.blocking_reasons


def test_pure_production_gate_requires_all_core_metrics() -> None:
    scope = EvaluationScope(
        run_purpose="production_decision",
        requested_horizons=("3y", "5y"),
        cohort_window={"start": None, "end": None},
        required_asofs=("2020-01-31",),
        required_metrics=("selection_rank_top5", "er_calibration"),
    )
    decision = decide_authority(scope, (_cohort("3y"), _cohort("5y")))
    assert decision.production_change_allowed is False
    assert any(reason.startswith("missing_core_metrics:") for reason in decision.blocking_reasons)


def test_required_metric_must_be_resolved_for_each_long_cohort() -> None:
    incomplete = CohortIntegrity(
        asof="2020-01-31",
        horizon="5y",
        integrity_status="eligible",
        metric_statuses={
            "selection_rank_top5": "eligible",
            "selection_rank_top10": "eligible",
            "er_calibration": "unresolved",
        },
    )
    decision = decide_authority(_scope(), (_cohort("3y"), incomplete))
    assert decision.production_change_allowed is False
    assert "metric_unresolved:er_calibration" in decision.blocking_reasons


def test_missing_required_metric_status_is_unresolved() -> None:
    missing_level = CohortIntegrity(
        asof="2020-01-31",
        horizon="5y",
        integrity_status="eligible",
        metric_statuses={
            "selection_rank_top5": "eligible",
            "selection_rank_top10": "eligible",
            "er_calibration": "eligible",
        },
    )
    required = (
        "selection_rank_top5",
        "selection_rank_top10",
        "er_calibration",
        "er_level_calibration",
    )
    scope = EvaluationScope(
        run_purpose="production_decision",
        requested_horizons=("3y", "5y"),
        cohort_window={"start": None, "end": None},
        required_asofs=("2020-01-31",),
        required_metrics=required,
    )
    complete = CohortIntegrity(
        asof="2020-01-31",
        horizon="3y",
        integrity_status="eligible",
        metric_statuses=dict.fromkeys(required, "eligible"),
    )

    decision = decide_authority(scope, (complete, missing_level))

    assert decision.production_change_allowed is False
    assert "metric_unresolved:er_level_calibration" in decision.blocking_reasons


def test_optional_er_level_metric_can_be_explicitly_required() -> None:
    required = (
        "selection_rank_top5",
        "selection_rank_top10",
        "er_calibration",
        "er_level_calibration",
    )
    scope = EvaluationScope(
        run_purpose="production_decision",
        requested_horizons=("3y", "5y"),
        cohort_window={"start": None, "end": None},
        required_asofs=("2020-01-31",),
        required_metrics=required,
    )
    cohorts = tuple(
        CohortIntegrity(
            asof="2020-01-31",
            horizon=horizon,
            integrity_status="eligible",
            metric_statuses=dict.fromkeys(required, "eligible"),
        )
        for horizon in ("3y", "5y")
    )

    decision = decide_authority(scope, cohorts)

    assert decision.production_change_allowed is True


def test_normalized_per_metric_can_be_explicitly_required() -> None:
    required = (
        "selection_rank_top5",
        "selection_rank_top10",
        "er_calibration",
        "normalized_per_3fy",
    )
    scope = EvaluationScope(
        run_purpose="production_decision",
        requested_horizons=("3y", "5y"),
        cohort_window={"start": None, "end": None},
        required_asofs=("2020-01-31",),
        required_metrics=required,
    )
    cohorts = tuple(
        CohortIntegrity(
            asof="2020-01-31",
            horizon=horizon,
            integrity_status="eligible",
            metric_statuses=dict.fromkeys(required, "eligible"),
        )
        for horizon in ("3y", "5y")
    )

    decision = decide_authority(scope, cohorts)

    assert decision.production_change_allowed is True


def test_margin_short_metric_can_be_explicitly_required() -> None:
    required = (
        "selection_rank_top5",
        "selection_rank_top10",
        "er_calibration",
        "margin_short_to_adv",
    )
    scope = EvaluationScope(
        run_purpose="production_decision",
        requested_horizons=("3y", "5y"),
        cohort_window={"start": None, "end": None},
        required_asofs=("2020-01-31",),
        required_metrics=required,
    )
    cohorts = tuple(
        CohortIntegrity(
            asof="2020-01-31",
            horizon=horizon,
            integrity_status="eligible",
            metric_statuses=dict.fromkeys(required, "eligible"),
        )
        for horizon in ("3y", "5y")
    )

    decision = decide_authority(scope, cohorts)

    assert decision.production_change_allowed is True


def test_blocking_reasons_stay_bounded_as_the_panel_count_grows() -> None:
    # One reason per cohort would grow with the panel count and bury the few causes a
    # reader can act on, and the same growth would turn the histogram into ones.
    asofs = tuple(f"2020-{month:02d}-28" for month in range(1, 13))
    scope = EvaluationScope(
        run_purpose="production_decision",
        requested_horizons=("3y", "5y"),
        cohort_window={"start": None, "end": None},
        required_asofs=asofs,
        required_metrics=("selection_rank_top5", "selection_rank_top10", "er_calibration"),
    )
    cohorts = tuple(
        CohortIntegrity(
            asof=asof,
            horizon=horizon,
            integrity_status="blocked",
            metric_statuses=dict.fromkeys(scope.required_metrics, "unresolved"),
            blocking_reasons=("survivorship",),
        )
        for asof in asofs
        for horizon in ("3y", "5y")
    )

    decision = decide_authority(scope, cohorts)

    assert set(decision.blocking_reasons) == {
        "integrity_blocked",
        "survivorship",
        "metric_unresolved:selection_rank_top5",
        "metric_unresolved:selection_rank_top10",
        "metric_unresolved:er_calibration",
    }
    assert decision.missing_cohorts == ()


def test_calendar_month_end_and_leap_day_are_clamped() -> None:
    assert add_months_clamped(date(2024, 1, 31), 1).isoformat() == "2024-02-29"
    assert add_months_clamped(date(2024, 2, 29), 12).isoformat() == "2025-02-28"
