"""保存済み成長モデルの期間・比較・出所を読む。候補政策や再計算は持たない。"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date
from typing import Literal, cast

from pydantic import JsonValue

from baibai_engine.machine_analysis.models.company_growth.calculations import (
    native_comparison_priors,
)
from baibai_engine.machine_analysis.models.company_growth.models import (
    Change,
    CompanyGrowth,
    Measurement,
    Metric,
    Observation,
    PeriodState,
    has_cohort_comparisons,
)
from baibai_engine.machine_analysis.models.company_growth.pdf_selection import (
    pdf_comparison_prior,
)
from baibai_engine.machine_analysis.observations.financials.financial_documents import (
    AccountingPeriodKey,
)

type PriorEvidenceIssue = Literal["outside_saved_window", "missing_or_invalid_saved_prior"]


class CompanyGrowthReader:
    def __init__(self, payload: dict[str, JsonValue] | None) -> None:
        self.model = CompanyGrowth.model_validate(payload) if payload is not None else None
        self._payload = payload if payload is not None else {}

    @property
    def cohort_bound(self) -> bool:
        return self.model is not None and has_cohort_comparisons(self.model.calculation_method)

    @property
    def latest_period(self) -> PeriodState | None:
        # Keep accounting_period_identity order, including tied period ends.
        return self.model.periods[-1] if self.model is not None and self.model.periods else None

    @staticmethod
    def observation(state: PeriodState | None, measurement: Measurement) -> Observation | None:
        if state is None:
            return None
        return next(
            (item for item in reversed(state.observations) if item.measurement == measurement),
            None,
        )

    @property
    def latest_full_fy(self) -> Observation | None:
        if self.model is None:
            return None
        # Fix the latest FY before reading values; never search backwards for a good year.
        state = next(
            (item for item in reversed(self.model.periods) if item.fiscal_period == "FY"),
            None,
        )
        reported = self.observation(state, "reported")
        annual = self.observation(state, "annual_ttm")
        # Interval construction already verified the approximately-one-year span.
        if (
            state is not None
            and annual is not None
            and reported is not None
            and annual.period_start == reported.period_start
            and annual.period_end == reported.period_end == state.fiscal_year_end
            and annual.statement_basis in {"consolidated", "nonconsolidated"}
        ):
            return annual
        return None

    def comparisons_for(self, observations: Sequence[Observation]) -> tuple[Change, ...]:
        if self.model is None or not observations:
            return ()
        if self.cohort_bound:
            owned = {id(item) for item in observations}
            targets = {
                (state.cohort, item.measurement, item.period_start, item.period_end)
                for state in self.model.periods
                if state.cohort is not None
                for item in state.observations
                if id(item) in owned
            }
            return tuple(
                item
                for item in self.model.changes
                if (item.target_cohort, item.measurement, item.to_start, item.to_end) in targets
                and item.native_prior_cohorts is not None
                and (
                    not item.native_prior_cohorts
                    if item.source_sha256 is not None
                    else item.difference is None or len(item.native_prior_cohorts) == 1
                )
            )
        # 旧固定結果にはcohortがない。当時の表示を保ち、新たな根拠は与えない。
        intervals = {
            (item.measurement, item.period_start, item.period_end) for item in observations
        }
        return tuple(
            item
            for item in self.model.changes
            if (item.measurement, item.to_start, item.to_end) in intervals
        )

    def year_over_year(self, observation: Observation | None, metric: Metric) -> tuple[Change, ...]:
        if observation is None:
            return ()
        return tuple(
            item
            for item in self.comparisons_for((observation,))
            if item.metric == metric
            # A decoder default is not an explicitly saved comparison observation.
            and "comparison" in item.model_fields_set
        )

    def comparison_prior(self, observation: Observation, change: Change) -> tuple[Observation, ...]:
        if self.model is None:
            return ()
        if (self.cohort_bound or change.source_sha256 is not None) and not any(
            saved is change for saved in self.year_over_year(observation, change.metric)
        ):
            return ()
        return self._prior_observations(observation, change, self.model.periods)

    def _prior_observations(
        self, observation: Observation, change: Change, periods: Sequence[PeriodState]
    ) -> tuple[Observation, ...]:
        if change.source_sha256 is not None:
            selected = self.model.pdf_selection if self.model is not None else None
            if self.cohort_bound and (
                selected is None
                or selected.current is None
                or not selected.comparative_verified
                or selected.current.statement_basis != observation.statement_basis
                or selected.current.accounting_standard is None
                or selected.current.accounting_standard != observation.accounting_standard
            ):
                return ()
            prior = pdf_comparison_prior(selected, observation, change)
            return (prior,) if prior is not None else ()
        if self.cohort_bound:
            priors = native_comparison_priors(periods, change)
            return (
                priors
                if observation.statement_basis in {"consolidated", "nonconsolidated"}
                and observation.accounting_standard is not None
                and all(
                    item.statement_basis == observation.statement_basis
                    and item.accounting_standard == observation.accounting_standard
                    for item in priors
                )
                else ()
            )
        return tuple(
            item
            for state in periods
            for item in state.observations
            if item.measurement == observation.measurement
            and item.statement_basis == observation.statement_basis
            and item.period_start == change.from_start
            and item.period_end == change.from_end
        )

    def prior_evidence_issues(
        self, comparisons: Sequence[Change]
    ) -> tuple[PriorEvidenceIssue | None, ...]:
        """保存済みの比較を表示する際、明示の窓外と窓内の根拠欠けを区別する。"""
        if self.model is None or not self.cohort_bound or not comparisons:
            return (None,) * len(comparisons)
        states: dict[AccountingPeriodKey, list[PeriodState]] = {}
        for state in self.model.periods:
            if state.cohort is not None:
                states.setdefault(state.cohort, []).append(state)
        start = self.model.requested_start
        issues: list[PriorEvidenceIssue | None] = []
        for change in comparisons:
            if change.difference is None:
                issues.append(None)
                continue
            targets = (
                states.get(change.target_cohort, []) if change.target_cohort is not None else []
            )
            current = (
                self.observation(targets[0], change.measurement) if len(targets) == 1 else None
            )
            candidates = [
                period
                for key in change.native_prior_cohorts or ()
                for period in states.get(key, ())
            ]
            priors = (
                self._prior_observations(current, change, candidates) if current is not None else ()
            )
            if (
                priors
                and change.from_value is not None
                and all(
                    (value := item.metrics.get(change.metric)) is not None
                    and value.value == change.from_value
                    and value.documents
                    for item in priors
                )
            ):
                issues.append(None)
            elif (
                current is not None
                and change.source_sha256 is None
                and not candidates
                and start is not None
                and change.from_end is not None
                and change.from_end < start
            ):
                issues.append("outside_saved_window")
            else:
                issues.append("missing_or_invalid_saved_prior")
        return tuple(issues)

    def saved_observation(self, observation: Observation | None) -> dict[str, JsonValue]:
        # Evidence must retain absent fields and original number representations. Do not
        # model_dump the typed read back into an immutable generation or selection.
        if self.model is not None and observation is not None:
            periods = cast(list[dict[str, JsonValue]], self._payload.get("periods", []))
            for state, payload in zip(self.model.periods, periods, strict=True):
                saved = cast(list[dict[str, JsonValue]], payload["observations"])
                for item, raw in zip(state.observations, saved, strict=True):
                    if item is observation:
                        return raw
        return {}

    def saved_change(self, change: Change) -> dict[str, JsonValue]:
        if self.model is not None:
            saved = cast(list[dict[str, JsonValue]], self._payload.get("changes", []))
            for item, raw in zip(self.model.changes, saved, strict=True):
                if item is change:
                    return raw
        raise ValueError("comparison does not belong to this saved growth model")


def metric_value(
    observation: Observation | None, metric: Metric, *, disclosed_by: date | None = None
) -> float | None:
    value = observation.metrics.get(metric) if observation is not None else None
    if value is None:
        return None
    # Retrospective reads are deliberate. Callers explicitly choose a disclosure cutoff;
    # neither the observation's measurement nor its period implies one.
    if disclosed_by is not None and (
        not value.documents
        or any(document.disclosed_at > disclosed_by for document in value.documents)
    ):
        return None
    return value.value


def same_comparison_endpoints(
    left: dict[str, JsonValue], right: dict[str, JsonValue], *, cohort_bound: bool
) -> bool:
    """二つの保存比較が同じ当期・比較元を指すか。額から対応を作らない。"""
    if not all(
        left.get(key) is not None and left[key] == right.get(key)
        for key in ("from_start", "from_end", "to_start", "to_end")
    ):
        return False
    if not cohort_bound:
        return True
    if (
        left.get("target_cohort") is None
        or left["target_cohort"] != right.get("target_cohort")
        or left.get("measurement") != right.get("measurement")
    ):
        return False
    left_pdf, right_pdf = left.get("source_sha256"), right.get("source_sha256")
    if left_pdf is not None or right_pdf is not None:
        return left_pdf is not None and left_pdf == right_pdf
    native = left.get("native_prior_cohorts")
    return (
        isinstance(native, list)
        and len(native) == 1
        and native == right.get("native_prior_cohorts")
    )
