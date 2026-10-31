"""保存済み成長モデルの期間・比較・出所を読む。候補政策や再計算は持たない。"""

from __future__ import annotations

from datetime import date
from typing import cast

from pydantic import JsonValue

from baibai_engine.machine_analysis.models.company_growth.models import (
    Change,
    CompanyGrowth,
    Measurement,
    Metric,
    Observation,
    PeriodState,
)


class CompanyGrowthReader:
    def __init__(self, payload: dict[str, JsonValue] | None) -> None:
        self.model = CompanyGrowth.model_validate(payload) if payload is not None else None
        self._payload = payload if payload is not None else {}

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

    def year_over_year(self, observation: Observation | None, metric: Metric) -> tuple[Change, ...]:
        if self.model is None or observation is None:
            return ()
        return tuple(
            item
            for item in self.model.changes
            if item.metric == metric
            # A decoder default is not an explicitly saved comparison observation.
            and "comparison" in item.model_fields_set
            and item.measurement == observation.measurement
            and item.to_start == observation.period_start
            and item.to_end == observation.period_end
        )

    def comparison_prior(self, observation: Observation, change: Change) -> tuple[Observation, ...]:
        if self.model is None:
            return ()
        return tuple(
            item
            for state in self.model.periods
            for item in state.observations
            if item.measurement == observation.measurement
            and item.statement_basis == observation.statement_basis
            and item.period_start == change.from_start
            and item.period_end == change.from_end
        )

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
