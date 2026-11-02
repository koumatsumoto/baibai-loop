"""Pure arithmetic over selected source observations, never a second source history."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import replace
from datetime import date
from typing import assert_never

from baibai_engine.machine_analysis.observations.economic_indicators.contracts import (
    SeriesDefinition,
)
from baibai_engine.machine_analysis.observations.economic_indicators.formulas import (
    FORMULAS,
    DerivedFormula,
    MonthlyObservationDate,
)
from baibai_engine.machine_analysis.observations.economic_indicators.observations import (
    ObservationRecord,
)
from baibai_engine.machine_analysis.observations.economic_indicators.validation import (
    reading_observation,
)


def derive_series(
    series: SeriesDefinition,
    inputs: Mapping[str, Sequence[ObservationRecord]],
    *,
    start: date,
    end: date,
) -> tuple[ObservationRecord, ...]:
    """Compute only the requested window from inputs already selected by the reader.

    Missing input or an undefined arithmetic result stays missing, rather than
    falling back to a formerly stored derived value. A result's optional vintage
    describes its selected inputs, not a new provider publication or wall-clock read.
    """
    formula = FORMULAS[series.reading_key]
    if series.unit != formula.unit:
        raise ValueError(f"derived series {series.series_id} has inconsistent unit")
    selected = {key: inputs.get(key, ()) for key in formula.inputs}
    results = []
    for observed_at, aligned in sorted(_align(formula, selected).items()):
        if not start <= observed_at <= end or len(aligned) != len(formula.inputs):
            continue
        value = formula.compute({key: row.value for key, row in aligned.items()})
        if value is None:
            continue
        vintages = [row.vintage_at for row in aligned.values()]
        vintage = max(v for v in vintages if v is not None) if all(vintages) else None
        observation = reading_observation(
            series, observed_at=observed_at, value=value, vintage_at=vintage
        )
        # A monthly bundle can contain different observation dates. Its output
        # date and max vintage alone must not imply synchronous, equally fresh inputs.
        date_rule = formula.monthly_observation_date if formula.alignment == "monthly" else "exact"
        notes = [f"derived_alignment={formula.alignment}; output_date_rule={date_rule}"]
        for key in formula.inputs:
            row = aligned[key]
            notes.append(
                f"input[{key}]: observed_at={row.observed_at.isoformat()}; "
                f"value={row.value!r}; unit={row.unit}; "
                f"vintage_at={row.vintage_at.isoformat() if row.vintage_at else 'unknown'}; "
                f"source_url={row.source_url}"
            )
            # Input flags remain attributed: a negative component is not a claim
            # that the derived output is negative. Keep source limitations as well.
            notes.extend(f"input[{key}] flag: {flag}" for flag in row.quality_flags)
            notes.extend(f"input[{key}] note: {note}" for note in row.notes)
        flags = (
            ("mixed_input_dates",) if len({row.observed_at for row in aligned.values()}) > 1 else ()
        )
        results.append(replace(observation, notes=tuple(notes), quality_flags=flags))
    return tuple(results)


def _align(
    formula: DerivedFormula, inputs: Mapping[str, Sequence[ObservationRecord]]
) -> dict[date, dict[str, ObservationRecord]]:
    """Pair input observations into bundles while retaining source availability."""
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
    # that month, i.e. the month-end reading for a daily input). Each formula
    # declares whether its output keeps the canonical month-start grid or uses
    # the latest selected input date to avoid backdating within-month market data.
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
