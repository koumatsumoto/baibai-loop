"""Measure whether empirical-change evidence is complete enough for human review."""

from __future__ import annotations

from dataclasses import asdict, dataclass

from ..discovery.review_set import APPROACH_IDS
from .horizons import HORIZONS

RunPurpose = str
EvidenceStatus = str
DecisionSubject = str
ESTIMATOR_POLICY_SUBJECT = "estimator_policy"
CANDIDATE_DISCOVERY_APPROACH_SUBJECT = "candidate_discovery_approach"
CANDIDATE_DISCOVERY_UNION_SUBJECT = "candidate_discovery_nomination_union"
DECISION_SUBJECTS = (
    ESTIMATOR_POLICY_SUBJECT,
    CANDIDATE_DISCOVERY_APPROACH_SUBJECT,
    CANDIDATE_DISCOVERY_UNION_SUBJECT,
)
CANDIDATE_DISCOVERY_UNION_FIDELITY_METRIC = "candidate_discovery_nomination_union_fidelity"
ESTIMATOR_POLICY_MANDATORY_METRICS = ("er_calibration",)
UNION_MANDATORY_METRICS = (
    "review_set_all",
    CANDIDATE_DISCOVERY_UNION_FIDELITY_METRIC,
    *(f"candidate_discovery_approach_{item.replace('-', '_')}_fidelity" for item in APPROACH_IDS),
)


def candidate_discovery_approach_top20_metric(approach: str) -> str:
    """Return the outcome metric for one approach's complete nomination depth."""
    return f"approach_{approach.replace('-', '_')}_top20"


def candidate_discovery_approach_fidelity_metric(approach: str) -> str:
    """Return the internal metric that proves one production approach was replayed."""
    return f"candidate_discovery_approach_{approach.replace('-', '_')}_fidelity"


KNOWN_METRICS = frozenset(
    (
        "review_set_all",
        "pure_er_top5",
        "pure_er_top10",
        "pure_er_top20",
        "er_calibration",
        "er_level_calibration",
        "margin_short_to_adv",
        "normalized_per_3fy",
        CANDIDATE_DISCOVERY_UNION_FIDELITY_METRIC,
        *(candidate_discovery_approach_top20_metric(item) for item in APPROACH_IDS),
        *(candidate_discovery_approach_fidelity_metric(item) for item in APPROACH_IDS),
    )
)


@dataclass(frozen=True, slots=True)
class EvaluationScope:
    run_purpose: RunPurpose
    requested_horizons: tuple[str, ...]
    cohort_window: dict[str, str | None]
    required_asofs: tuple[str, ...]
    required_metrics: tuple[str, ...]
    decision_subject: DecisionSubject = ESTIMATOR_POLICY_SUBJECT
    candidate_discovery_approaches: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class CohortIntegrity:
    asof: str
    horizon: str
    integrity_status: EvidenceStatus
    metric_statuses: dict[str, EvidenceStatus]
    blocking_reasons: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class EvidenceReadiness:
    """Why evidence is or is not complete enough for a human decision.

    ``blocking_reasons`` names classes of problem, never single cohorts: one entry per
    cohort would grow with the panel count and bury the handful of causes a reader can
    act on. Per-cohort detail belongs to that cohort's own result. ``missing_cohorts``
    is the exception, because a cohort with no result has nowhere else to be named.
    """

    evidence_status: EvidenceStatus
    evidence_complete: bool
    blocking_reasons: tuple[str, ...]
    missing_cohorts: tuple[str, ...] = ()

    def payload(self) -> dict[str, object]:
        return asdict(self)


def subject_mandatory_metrics(scope: EvaluationScope) -> tuple[str, ...]:
    """Return the evidence metrics an operator cannot omit for this subject."""
    metrics: list[str]
    if scope.decision_subject == ESTIMATOR_POLICY_SUBJECT:
        metrics = list(ESTIMATOR_POLICY_MANDATORY_METRICS)
    elif scope.decision_subject == CANDIDATE_DISCOVERY_UNION_SUBJECT:
        metrics = list(UNION_MANDATORY_METRICS)
    else:
        metrics = []
    if (
        scope.decision_subject == CANDIDATE_DISCOVERY_APPROACH_SUBJECT
        and len(scope.candidate_discovery_approaches) == 1
        and scope.candidate_discovery_approaches[0] in APPROACH_IDS
    ):
        approach = scope.candidate_discovery_approaches[0]
        metrics.extend(
            (
                "review_set_all",
                CANDIDATE_DISCOVERY_UNION_FIDELITY_METRIC,
                candidate_discovery_approach_top20_metric(approach),
                candidate_discovery_approach_fidelity_metric(approach),
            )
        )
    return tuple(dict.fromkeys(metrics))


def effective_required_metrics(scope: EvaluationScope) -> tuple[str, ...]:
    """Combine subject-mandatory evidence with optional preregistered guardrails."""
    return tuple(dict.fromkeys((*subject_mandatory_metrics(scope), *scope.required_metrics)))


def evaluate_evidence_readiness(
    scope: EvaluationScope, cohorts: tuple[CohortIntegrity, ...]
) -> EvidenceReadiness:
    """Report completeness without deciding whether a method should change."""
    reasons: list[str] = []
    unknown = [name for name in scope.requested_horizons if name not in HORIZONS]
    if unknown:
        reasons.append(f"unknown_horizon:{','.join(sorted(unknown))}")
    if scope.run_purpose != "empirical_change_evidence":
        reasons.append("diagnostic_run_has_no_evidence_readiness")
    required_horizons = ("3y", "5y")
    if scope.run_purpose == "empirical_change_evidence":
        missing_horizons = [
            name for name in required_horizons if name not in scope.requested_horizons
        ]
        if missing_horizons:
            reasons.append(f"missing_required_horizons:{','.join(missing_horizons)}")
    if not scope.required_asofs:
        reasons.append("missing_required_asofs")
    if scope.run_purpose == "empirical_change_evidence":
        if scope.decision_subject not in DECISION_SUBJECTS:
            reasons.append(f"unknown_decision_subject:{scope.decision_subject}")
        elif scope.decision_subject == CANDIDATE_DISCOVERY_APPROACH_SUBJECT:
            if len(scope.candidate_discovery_approaches) != 1:
                reasons.append("candidate_discovery_approach_must_be_exactly_one")
            else:
                unknown_approaches = set(scope.candidate_discovery_approaches) - set(APPROACH_IDS)
                if unknown_approaches:
                    reasons.append(
                        "unknown_candidate_discovery_approach:"
                        f"{','.join(sorted(unknown_approaches))}"
                    )
        elif scope.candidate_discovery_approaches:
            reasons.append("candidate_discovery_approach_not_allowed_for_subject")
        unknown_metrics = set(scope.required_metrics) - KNOWN_METRICS
        if unknown_metrics:
            reasons.append(f"unknown_required_metrics:{','.join(sorted(unknown_metrics))}")

    required_metrics = effective_required_metrics(scope)
    index = {(item.asof, item.horizon): item for item in cohorts}
    missing: list[str] = []
    for asof in scope.required_asofs:
        for horizon in required_horizons:
            item = index.get((asof, horizon))
            if item is None:
                reasons.append("missing_cohort")
                missing.append(f"{asof}:{horizon}")
                continue
            if item.integrity_status != "eligible":
                reasons.append(f"integrity_{item.integrity_status}")
            reasons.extend(item.blocking_reasons)
            for metric in required_metrics:
                if item.metric_statuses.get(metric) != "eligible":
                    reasons.append(f"metric_unresolved:{metric}")
    if not reasons:
        return EvidenceReadiness(
            evidence_status="eligible",
            evidence_complete=True,
            blocking_reasons=(),
        )
    status: EvidenceStatus = (
        "unresolved" if all("missing_" in item for item in reasons) else "blocked"
    )
    return EvidenceReadiness(
        evidence_status=status,
        evidence_complete=False,
        blocking_reasons=tuple(dict.fromkeys(reasons)),
        missing_cohorts=tuple(missing),
    )
