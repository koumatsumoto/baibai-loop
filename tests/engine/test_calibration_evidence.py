from __future__ import annotations

from datetime import date

from baibai_engine.screening.calibration.evidence import (
    CANDIDATE_DISCOVERY_APPROACH_SUBJECT,
    CANDIDATE_DISCOVERY_UNION_FIDELITY_METRIC,
    CANDIDATE_DISCOVERY_UNION_SUBJECT,
    CohortIntegrity,
    EvaluationScope,
    candidate_discovery_approach_fidelity_metric,
    candidate_discovery_approach_top20_metric,
    effective_required_metrics,
    evaluate_evidence_readiness,
)
from baibai_engine.screening.calibration.horizons import add_months_clamped


def _scope(
    *,
    purpose: str = "empirical_change_evidence",
    subject: str = "estimator_policy",
    approach: str | None = None,
    extra_metrics: tuple[str, ...] = (),
) -> EvaluationScope:
    return EvaluationScope(
        run_purpose=purpose,
        requested_horizons=("3m", "6m", "1y", "3y", "5y"),
        cohort_window={"start": "2020-01-31", "end": "2020-01-31"},
        required_asofs=("2020-01-31",),
        required_metrics=extra_metrics,
        decision_subject=subject,
        candidate_discovery_approaches=((approach,) if approach is not None else ()),
    )


def _cohorts(scope: EvaluationScope, *, status: str = "eligible") -> tuple[CohortIntegrity, ...]:
    metrics = effective_required_metrics(scope)
    return tuple(
        CohortIntegrity(
            asof="2020-01-31",
            horizon=horizon,
            integrity_status=status,
            metric_statuses=dict.fromkeys(metrics, status),
        )
        for horizon in ("3y", "5y")
    )


def test_complete_evidence_does_not_depend_on_effect_direction() -> None:
    scope = _scope()
    readiness = evaluate_evidence_readiness(scope, _cohorts(scope))
    assert readiness.evidence_complete is True
    assert readiness.evidence_status == "eligible"
    assert set(readiness.payload()) == {
        "evidence_status",
        "evidence_complete",
        "blocking_reasons",
        "missing_cohorts",
    }


def test_short_horizons_do_not_complete_evidence() -> None:
    scope = _scope()
    readiness = evaluate_evidence_readiness(scope, ())
    assert readiness.evidence_complete is False
    assert readiness.missing_cohorts == ("2020-01-31:3y", "2020-01-31:5y")


def test_diagnostic_has_no_evidence_readiness() -> None:
    scope = _scope(purpose="diagnostic")
    readiness = evaluate_evidence_readiness(scope, _cohorts(scope))
    assert readiness.evidence_complete is False
    assert "diagnostic_run_has_no_evidence_readiness" in readiness.blocking_reasons


def test_estimator_policy_requires_only_er_calibration_by_subject() -> None:
    assert effective_required_metrics(_scope()) == ("er_calibration",)


def test_nomination_union_mandatory_metrics_cannot_be_omitted() -> None:
    scope = _scope(subject=CANDIDATE_DISCOVERY_UNION_SUBJECT)
    required = effective_required_metrics(scope)
    assert required[:2] == (
        "review_set_all",
        CANDIDATE_DISCOVERY_UNION_FIDELITY_METRIC,
    )
    assert len(required) == 6
    incomplete = tuple(
        CohortIntegrity(
            asof=item.asof,
            horizon=item.horizon,
            integrity_status=item.integrity_status,
            metric_statuses={"pure_er_top20": "eligible"},
        )
        for item in _cohorts(scope)
    )
    readiness = evaluate_evidence_readiness(scope, incomplete)
    assert readiness.evidence_complete is False
    assert "metric_unresolved:review_set_all" in readiness.blocking_reasons


def test_approach_mandatory_metrics_include_direct_and_union_outcomes() -> None:
    approach = "asset-value"
    scope = _scope(subject=CANDIDATE_DISCOVERY_APPROACH_SUBJECT, approach=approach)
    assert effective_required_metrics(scope) == (
        "review_set_all",
        CANDIDATE_DISCOVERY_UNION_FIDELITY_METRIC,
        candidate_discovery_approach_top20_metric(approach),
        candidate_discovery_approach_fidelity_metric(approach),
    )
    assert evaluate_evidence_readiness(scope, _cohorts(scope)).evidence_complete is True


def test_er_top20_is_diagnostic_not_candidate_discovery_requirement() -> None:
    scope = _scope(subject=CANDIDATE_DISCOVERY_UNION_SUBJECT)
    cohorts = tuple(
        CohortIntegrity(
            asof=item.asof,
            horizon=item.horizon,
            integrity_status=item.integrity_status,
            metric_statuses={
                **item.metric_statuses,
                "pure_er_top20": "unresolved",
            },
        )
        for item in _cohorts(scope)
    )
    assert evaluate_evidence_readiness(scope, cohorts).evidence_complete is True


def test_operator_metric_is_an_additional_guardrail() -> None:
    scope = _scope(extra_metrics=("er_level_calibration",))
    incomplete = tuple(
        CohortIntegrity(
            asof=item.asof,
            horizon=item.horizon,
            integrity_status=item.integrity_status,
            metric_statuses={"er_calibration": "eligible"},
        )
        for item in _cohorts(scope)
    )
    readiness = evaluate_evidence_readiness(scope, incomplete)
    assert readiness.evidence_complete is False
    assert "metric_unresolved:er_level_calibration" in readiness.blocking_reasons


def test_unknown_subject_and_approach_fail_closed() -> None:
    unknown_subject = _scope(subject="unknown")
    unknown_approach = _scope(subject=CANDIDATE_DISCOVERY_APPROACH_SUBJECT, approach="unknown")
    assert (
        "unknown_decision_subject:unknown"
        in evaluate_evidence_readiness(unknown_subject, _cohorts(unknown_subject)).blocking_reasons
    )
    assert any(
        reason.startswith("unknown_candidate_discovery_approach:")
        for reason in evaluate_evidence_readiness(
            unknown_approach, _cohorts(unknown_approach)
        ).blocking_reasons
    )


def test_calendar_month_end_and_leap_day_are_clamped() -> None:
    assert add_months_clamped(date(2024, 1, 31), 1).isoformat() == "2024-02-29"
    assert add_months_clamped(date(2024, 2, 29), 12).isoformat() == "2025-02-28"
