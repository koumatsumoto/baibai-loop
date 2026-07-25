from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import date
from typing import assert_never

from ..db import ObservationRecord
from ..definitions import SeriesDefinition
from .base import (
    FetchContext,
    HttpSession,
    IndicatorsProviderError,
    ProviderSpec,
    record_observation,
)
from .formulas import (
    FORMULAS,
    DerivedComputationError,
    DerivedFormula,
    MonthlyObservationDate,
)


class DerivedProvider:
    """Compute a derived series from other stored series (no network).

    ``provider_series_id`` selects a formula in ``formulas.FORMULAS``. The
    provider reads each input series from the store (via the context's
    store_reader, bound to the live connection), aligns them per the formula's
    alignment, and emits an observation for every aligned date where every input
    is present. Because it reads the store, it runs after the base series are
    refreshed (the batch orders ``local`` providers after ``http`` ones).
    """

    # A local provider has no external source to define a floor: the history of a
    # derived series is exactly the overlap of its inputs' histories. The floor is
    # therefore set before any base series begins, so `--all-history` recomputes the
    # whole overlap instead of refusing (without it, a derived series could never be
    # rebuilt and would stay as shallow as the last rolling window).
    spec = ProviderSpec(
        name="derived",
        kind="local",
        all_history_start=date(1970, 1, 1),
        # A rebuild of a formula whose alignment changes recomputes its complete
        # overlap and replaces the old grid. Other formulas keep ordinary
        # append/revision semantics and are not exposed to destructive replacement.
        range_replacement_overrides={"us.erp": "all_vintages"},
    )
    name = spec.name

    def fetch(
        self,
        series: SeriesDefinition,
        *,
        start: date,
        end: date,
        session: HttpSession,
        context: FetchContext | None = None,
    ) -> list[ObservationRecord]:
        formula = FORMULAS.get(series.provider_series_id)
        if formula is None:
            supported = ", ".join(sorted(FORMULAS))
            raise IndicatorsProviderError(
                f"derived has no formula for {series.provider_series_id!r}; supported: {supported}"
            )
        if context is None or context.store_reader is None:
            raise IndicatorsProviderError(
                f"derived series {series.series_id} needs a store reader to read its inputs"
            )
        if series.unit != formula.unit:
            raise IndicatorsProviderError(
                f"derived series {series.series_id} unit {series.unit!r} does not match "
                f"formula unit {formula.unit!r}"
            )
        inputs: dict[str, Sequence[ObservationRecord]] = {}
        for input_id in formula.inputs:
            observations = context.store_reader(input_id, start, end)
            if not observations:
                raise IndicatorsProviderError(
                    f"derived series {series.series_id} input {input_id} has no observations "
                    f"in [{start}, {end}]; refresh the base series first"
                )
            inputs[input_id] = observations

        results: list[ObservationRecord] = []
        for observed_at, aligned in sorted(_align(formula, inputs).items()):
            # Keep only dates present in every input so a partial period never
            # yields a half-computed value.
            if len(aligned) != len(formula.inputs):
                continue
            try:
                value = formula.evaluate(
                    {input_id: observation.value for input_id, observation in aligned.items()}
                )
            except DerivedComputationError as exc:
                raise IndicatorsProviderError(
                    f"derived series {series.series_id} {observed_at}: {exc}"
                ) from exc
            if value is None:
                continue
            results.append(record_observation(series, observed_at=observed_at, value=value))
        return results


def _align(
    formula: DerivedFormula, inputs: Mapping[str, Sequence[ObservationRecord]]
) -> dict[date, dict[str, ObservationRecord]]:
    """Pair input observations into dated bundles without losing availability."""
    match formula.alignment:
        case "exact":
            return _align_exact(inputs)
        case "monthly":
            return _align_monthly(
                inputs,
                observation_date=formula.monthly_observation_date,
            )
        case _:  # pragma: no cover - exhaustiveness guard over the Alignment literal
            assert_never(formula.alignment)


def _align_exact(
    inputs: Mapping[str, Sequence[ObservationRecord]],
) -> dict[date, dict[str, ObservationRecord]]:
    by_date: dict[date, dict[str, ObservationRecord]] = {}
    for input_id, observations in inputs.items():
        for observation in observations:
            by_date.setdefault(observation.observed_at, {})[input_id] = observation
    return by_date


def _align_monthly(
    inputs: Mapping[str, Sequence[ObservationRecord]],
    *,
    observation_date: MonthlyObservationDate,
) -> dict[date, dict[str, ObservationRecord]]:
    # Fold each input to one value per calendar month (its latest observed_at in
    # that month, i.e. the month-end reading for a daily input). The output date
    # is the latest selected input date, so a month-end value is never backdated
    # to the first of its month. ``vintage_at`` separately carries when every
    # selected input became available.
    by_period: dict[tuple[int, int], dict[str, ObservationRecord]] = {}
    for input_id, observations in inputs.items():
        latest_in_month: dict[tuple[int, int], ObservationRecord] = {}
        for observation in observations:
            key = (observation.observed_at.year, observation.observed_at.month)
            current = latest_in_month.get(key)
            if current is None or observation.observed_at > current.observed_at:
                latest_in_month[key] = observation
        for (year, month), observation in latest_in_month.items():
            by_period.setdefault((year, month), {})[input_id] = observation
    if observation_date == "latest_input":
        return {
            max(observation.observed_at for observation in aligned.values()): aligned
            for aligned in by_period.values()
        }
    return {date(year, month, 1): aligned for (year, month), aligned in by_period.items()}
