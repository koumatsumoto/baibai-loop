"""Pure authority gate for empirical estimator-policy changes."""

from __future__ import annotations

from dataclasses import asdict, dataclass

from .horizons import HORIZONS

RunPurpose = str
EvidenceStatus = str
PRODUCTION_REQUIRED_METRICS = (
    "recommended_rank_top5",
    "recommended_rank_top10",
    "er_calibration",
)
KNOWN_METRICS = frozenset(
    (
        *PRODUCTION_REQUIRED_METRICS,
        "er_level_calibration",
        "selection_rank_top5",
        "selection_rank_top10",
        "margin_deadline_gate_top10",
    )
)


@dataclass(frozen=True, slots=True)
class EvaluationScope:
    run_purpose: RunPurpose
    requested_horizons: tuple[str, ...]
    cohort_window: dict[str, str | None]
    required_asofs: tuple[str, ...]
    required_metrics: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class CohortIntegrity:
    asof: str
    horizon: str
    integrity_status: EvidenceStatus
    metric_statuses: dict[str, EvidenceStatus]
    blocking_reasons: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class AuthorityDecision:
    """Why production authority is or is not granted.

    ``blocking_reasons`` names classes of problem, never single cohorts: one entry per
    cohort would grow with the panel count and bury the handful of causes a reader can
    act on. Per-cohort detail belongs to that cohort's own result. ``missing_cohorts``
    is the exception, because a cohort with no result has nowhere else to be named.
    """

    authority: str
    evidence_status: EvidenceStatus
    production_change_allowed: bool
    blocking_reasons: tuple[str, ...]
    missing_cohorts: tuple[str, ...] = ()

    def payload(self) -> dict[str, object]:
        return asdict(self)


def decide_authority(
    scope: EvaluationScope, cohorts: tuple[CohortIntegrity, ...]
) -> AuthorityDecision:
    """Allow empirical production changes only for complete 3y/5y evidence."""
    reasons: list[str] = []
    unknown = [name for name in scope.requested_horizons if name not in HORIZONS]
    if unknown:
        reasons.append(f"unknown_horizon:{','.join(sorted(unknown))}")
    if scope.run_purpose != "production_decision":
        reasons.append("diagnostic_run_has_no_production_authority")
    required_horizons = ("3y", "5y")
    if scope.run_purpose == "production_decision":
        missing_horizons = [
            name for name in required_horizons if name not in scope.requested_horizons
        ]
        if missing_horizons:
            reasons.append(f"missing_required_horizons:{','.join(missing_horizons)}")
    if not scope.required_asofs:
        reasons.append("missing_required_asofs")
    if not scope.required_metrics:
        reasons.append("missing_required_metrics")
    if scope.run_purpose == "production_decision":
        unknown_metrics = set(scope.required_metrics) - KNOWN_METRICS
        missing_core_metrics = set(PRODUCTION_REQUIRED_METRICS) - set(scope.required_metrics)
        if unknown_metrics:
            reasons.append(f"unknown_required_metrics:{','.join(sorted(unknown_metrics))}")
        if missing_core_metrics:
            reasons.append(f"missing_core_metrics:{','.join(sorted(missing_core_metrics))}")

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
            for metric in scope.required_metrics:
                if item.metric_statuses.get(metric) != "eligible":
                    reasons.append(f"metric_unresolved:{metric}")
    if not reasons:
        return AuthorityDecision(
            authority="production_decision_evidence",
            evidence_status="eligible",
            production_change_allowed=True,
            blocking_reasons=(),
        )
    status: EvidenceStatus = (
        "unresolved" if all("missing_" in item for item in reasons) else "blocked"
    )
    return AuthorityDecision(
        authority="production_decision_evidence",
        evidence_status=status,
        production_change_allowed=False,
        blocking_reasons=tuple(dict.fromkeys(reasons)),
        missing_cohorts=tuple(missing),
    )
