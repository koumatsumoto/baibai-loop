"""Construct a transient reading from its source inputs, without storing a new fact."""

from __future__ import annotations

from datetime import date, datetime
from math import isfinite

from baibai_engine.machine_analysis.models.market_context.indicators.definitions import SeriesDefinition
from baibai_engine.machine_analysis.models.market_context.indicators.observations import ObservationRecord


class UnavailableObservationError(ValueError):
    """A selected source receipt cannot support this series; do not use older values."""


def reading_observation(
    series: SeriesDefinition,
    *,
    observed_at: date,
    value: float,
    vintage_at: datetime | None,
) -> ObservationRecord:
    if not isfinite(value):
        raise ValueError(f"computed series {series.series_id} has a non-finite result")
    if (series.plausible_min is not None and value < series.plausible_min) or (
        series.plausible_max is not None and value > series.plausible_max
    ):
        raise ValueError(f"computed series {series.series_id} is outside plausible range")
    return ObservationRecord(
        series_id=series.series_id,
        observed_at=observed_at,
        value=value,
        unit=series.unit,
        source_url=series.source_url,
        period_start=observed_at,
        period_end=observed_at,
        vintage_at=vintage_at,
    )
