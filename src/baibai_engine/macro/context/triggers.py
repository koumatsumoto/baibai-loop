"""Check a published report's invalidation conditions against the L1 history.

A report's freshness rule is a clock: past a threshold in days the reader is warned. A
clock cannot see a regime break — a report written three days ago can be obsolete before
lunch — so the monitoring section states what would make its view wrong, and this module
asks the store whether any of it has happened. The daily reading and the monthly
judgment close on each other here: the machine measures, the report says what would
change the reading of it.

The discipline is deliberately weaker than the scorecard's. A scorecard settles a
scenario, so it demands proof — a provider run covering the window, a vintage on the
matching observation, a hard error rather than a guess. A trigger only asks a human to
look again, so a series that has not printed yet is reported as such and blocks nothing.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Sequence
from datetime import date, timedelta
from pathlib import Path
from typing import Literal, assert_never

from pydantic import BaseModel, ConfigDict, Field

from baibai_engine.macro.indicators import db as indicators_db
from baibai_engine.macro.indicators.db import ObservationRecord
from baibai_engine.macro.indicators.definitions import load_definitions
from baibai_engine.macro.reading.reader import ObservationReader, build_store_observation_reader

from .models import MacroContextDocument, TriggerCondition
from .scorecard import load_context_document

type TriggerStatus = Literal["fired", "quiet", "not_evaluable"]


class _OutputModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class TriggerObservation(_OutputModel):
    observed_at: date
    value: float = Field(allow_inf_nan=False)
    unit: str


class TriggerResult(_OutputModel):
    point_index: int = Field(ge=1)
    event: str
    condition_index: int = Field(ge=1)
    series_id: str
    comparison: Literal["below", "at_or_below", "above", "at_or_above"]
    threshold: float = Field(allow_inf_nan=False)
    status: TriggerStatus
    # The first observation that met the condition when fired, the latest one seen when
    # quiet, and nothing when the series has not printed inside the window.
    observation: TriggerObservation | None
    view_change: str


class TriggerEvaluation(_OutputModel):
    schema_version: Literal[1] = 1
    kind: Literal["macro-context-triggers"] = "macro-context-triggers"
    context_id: str
    context_as_of: date
    asof: date
    results: tuple[TriggerResult, ...]

    @property
    def fired(self) -> tuple[TriggerResult, ...]:
        return tuple(item for item in self.results if item.status == "fired")

    def payload(self) -> dict[str, object]:
        return self.model_dump(mode="json")


class TriggerEvaluationError(ValueError):
    pass


def evaluate_triggers(
    document: MacroContextDocument,
    *,
    reader: ObservationReader,
    known_series: frozenset[str],
    asof: date,
) -> TriggerEvaluation:
    """Evaluate every machine-checkable monitoring condition over (as_of, asof].

    The window opens the day after the report's as_of because the report already read
    everything up to it: a condition that the report's own closing observation satisfies
    was true when the view was written, not a change to it.

    A condition counts as fired when *any* observation in the window met it, not only
    the latest. The point is to notice that a regime line was crossed; a level that
    touched and came back is still a fact the author has to weigh.
    """

    if asof < document.as_of:
        raise TriggerEvaluationError(f"trigger asof {asof} predates context as_of {document.as_of}")
    start = document.as_of + timedelta(days=1)
    results: list[TriggerResult] = []
    for point_index, point in enumerate(document.monitoring_points, start=1):
        for condition_index, condition in enumerate(point.machine_conditions, start=1):
            observations = (
                reader(condition.series_id, start, asof)
                if start <= asof and condition.series_id in known_series
                else ()
            )
            results.append(
                _evaluate_condition(
                    point_index=point_index,
                    event=point.event,
                    view_change=point.view_change,
                    condition_index=condition_index,
                    condition=condition,
                    observations=observations,
                )
            )
    return TriggerEvaluation(
        context_id=document.context_id,
        context_as_of=document.as_of,
        asof=asof,
        results=tuple(results),
    )


def evaluate_triggers_from_stores(
    *,
    context_db: Path | None,
    indicators_db_path: Path,
    context_id: str,
    asof: date,
) -> TriggerEvaluation:
    document = load_context_document(context_db, context_id=context_id)
    definitions = load_definitions()
    connection = indicators_db.open_read_only_connection(indicators_db_path)
    try:
        return evaluate_triggers(
            document,
            reader=build_store_observation_reader(
                connection,
                series=definitions.series,
                vintage_cutoff=asof,
            ),
            known_series=frozenset(item.series_id for item in definitions.series),
            asof=asof,
        )
    finally:
        connection.close()


def evaluate_triggers_if_readable(
    *,
    context_db: Path | None,
    indicators_db_path: Path,
    context_id: str,
    asof: date,
) -> TriggerEvaluation | None:
    """Evaluate the report's conditions, or return None when no store can answer.

    Only an unreadable *store* degrades to None. A checkout that carries the application
    database without the indicator store has nothing to contradict the report with, which
    is an ordinary state — but a report that cannot be read is the failure that took the
    daily batch down once already, so it is raised rather than reported as "nothing
    fired". Every consumer of this shares the distinction; keeping it in one place is why
    the two callers cannot drift apart on which failures are survivable.
    """

    if not indicators_db_path.is_file():
        return None
    try:
        return evaluate_triggers_from_stores(
            context_db=context_db,
            indicators_db_path=indicators_db_path,
            context_id=context_id,
            asof=asof,
        )
    except (OSError, sqlite3.Error, indicators_db.IndicatorsSchemaError):
        return None


def fired_trigger_summaries(
    *,
    context_db: Path | None,
    indicators_db_path: Path,
    context_id: str,
    asof: date,
) -> tuple[str, ...] | None:
    """One line per fired condition, or None when no store could answer at all."""

    evaluation = evaluate_triggers_if_readable(
        context_db=context_db,
        indicators_db_path=indicators_db_path,
        context_id=context_id,
        asof=asof,
    )
    if evaluation is None:
        return None
    return tuple(
        f"{item.event}: {item.series_id} {item.comparison} {item.threshold:g}"
        + (
            ""
            if item.observation is None
            else f" ({item.observation.observed_at.isoformat()} = {item.observation.value:g})"
        )
        for item in evaluation.fired
    )


def _evaluate_condition(
    *,
    point_index: int,
    event: str,
    view_change: str,
    condition_index: int,
    condition: TriggerCondition,
    observations: Sequence[ObservationRecord],
) -> TriggerResult:
    first_met = next(
        (
            observation
            for observation in observations
            if _matches(
                observation.value,
                comparison=condition.comparison,
                threshold=condition.threshold,
            )
        ),
        None,
    )
    if first_met is not None:
        status: TriggerStatus = "fired"
        used: ObservationRecord | None = first_met
    elif observations:
        status = "quiet"
        used = observations[-1]
    else:
        status = "not_evaluable"
        used = None
    return TriggerResult(
        point_index=point_index,
        event=event,
        condition_index=condition_index,
        series_id=condition.series_id,
        comparison=condition.comparison,
        threshold=condition.threshold,
        status=status,
        observation=(
            None
            if used is None
            else TriggerObservation(
                observed_at=used.observed_at,
                value=used.value,
                unit=used.unit,
            )
        ),
        view_change=view_change,
    )


def _matches(
    value: float,
    *,
    comparison: Literal["below", "at_or_below", "above", "at_or_above"],
    threshold: float,
) -> bool:
    match comparison:
        case "below":
            return value < threshold
        case "at_or_below":
            return value <= threshold
        case "above":
            return value > threshold
        case "at_or_above":
            return value >= threshold
    assert_never(comparison)


__all__ = [
    "TriggerEvaluation",
    "TriggerEvaluationError",
    "TriggerObservation",
    "TriggerResult",
    "TriggerStatus",
    "evaluate_triggers",
    "evaluate_triggers_from_stores",
    "fired_trigger_summaries",
]
