from __future__ import annotations

from datetime import date

from ..db import ObservationRecord
from ..definitions import SeriesDefinition
from .base import (
    FetchContext,
    HttpSession,
    IndicatorsProviderError,
    ProviderSpec,
    record_observation,
)
from .formulas import FORMULAS, DerivedComputationError


class DerivedProvider:
    """Compute a derived series from other stored series (no network).

    ``provider_series_id`` selects a formula in ``formulas.FORMULAS``. The
    provider reads each input series from the store (via the context's
    store_reader, bound to the live connection), aligns them by observed_at, and
    emits an observation for every date present in *all* inputs. Because it reads
    the store, it runs after the base series are refreshed (the batch orders
    ``local`` providers after ``http`` ones).
    """

    spec = ProviderSpec(name="derived", kind="local")
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
        # observed_at -> {input_series_id -> value}; keep only dates present in
        # every input so a partial day never yields a half-computed value.
        by_date: dict[date, dict[str, float]] = {}
        for input_id in formula.inputs:
            observations = context.store_reader(input_id, start, end)
            if not observations:
                raise IndicatorsProviderError(
                    f"derived series {series.series_id} input {input_id} has no observations "
                    f"in [{start}, {end}]; refresh the base series first"
                )
            for observation in observations:
                by_date.setdefault(observation.observed_at, {})[input_id] = observation.value

        results: list[ObservationRecord] = []
        for observed_at in sorted(by_date):
            aligned = by_date[observed_at]
            if len(aligned) != len(formula.inputs):
                continue
            try:
                value = formula.evaluate(aligned)
            except DerivedComputationError as exc:
                raise IndicatorsProviderError(
                    f"derived series {series.series_id} {observed_at}: {exc}"
                ) from exc
            if value is None:
                continue
            results.append(record_observation(series, observed_at=observed_at, value=value))
        return results
