"""Transient L2 observations and derived values from the same retained L1 snapshot.

Only providers whose spec declares publication-date vintages are clamped to
the requested cutoff. This does not establish exact historical API availability.
Most bulk history stores the acquisition time as its vintage, not the original
publication time; clamping those providers would make
valid history disappear from snapshots whose as-of predates the backfill.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Mapping, Sequence
from datetime import date

from baibai_engine.data_collection.catalog import DOCUMENT_PROVIDERS
from baibai_engine.machine_analysis.observations.economic_indicators.contracts import (
    SeriesDefinition,
)
from baibai_engine.machine_analysis.observations.economic_indicators.derived import derive_series
from baibai_engine.machine_analysis.observations.economic_indicators.documents import (
    read_document_series,
)
from baibai_engine.machine_analysis.observations.economic_indicators.flows import (
    retained_publication_range,
)
from baibai_engine.machine_analysis.observations.economic_indicators.formulas import FORMULAS
from baibai_engine.machine_analysis.observations.economic_indicators.native import (
    read_native_series,
)
from baibai_engine.machine_analysis.observations.economic_indicators.observations import (
    ObservationReader,
    ObservationRecord,
)
from baibai_engine.machine_analysis.observations.economic_indicators.options import (
    OptionSeriesReader,
)
from baibai_engine.machine_analysis.observations.economic_indicators.read_contracts import (
    NATIVE_BULK_PROVIDERS,
    NATIVE_PROVIDERS,
)


def _derived_input_ranges(
    definition: SeriesDefinition, start: date, end: date
) -> dict[str, tuple[date, date]]:
    formula = FORMULAS[definition.reading_key]
    input_start = start.replace(day=1) if formula.alignment == "monthly" else start
    return dict.fromkeys(formula.inputs, (input_start, end))


def source_record_ranges(
    *,
    series: Sequence[SeriesDefinition],
    requested: Mapping[str, tuple[date, date]],
    vintage_cutoff: date | None = None,
) -> dict[str, tuple[date, date]]:
    """Native read bounds shared with calculation, without changing L1 acquisition.

    Shared sources use the enclosing union, which may include gaps between requests.
    Documents remain unbounded here: their latest unreadable/out-of-window evidence
    can affect selection. Flow bounds are publication dates, not observation dates.
    """
    definitions = {item.series_id: item for item in series}
    ranges: dict[str, tuple[date, date]] = {}

    def visit(series_id: str, start: date, end: date) -> None:
        definition = definitions[series_id]
        if definition.provider == "derived":
            for key, bounds in _derived_input_ranges(definition, start, end).items():
                visit(key, *bounds)
        elif definition.provider in DOCUMENT_PROVIDERS:
            return
        elif definition.provider in NATIVE_PROVIDERS:
            bounds = (
                retained_publication_range(vintage_cutoff or end)
                if definition.provider == "jquants_flows"
                else (start, end)
            )
            previous = ranges.get(definition.source_id, bounds)
            ranges[definition.source_id] = (
                min(previous[0], bounds[0]),
                max(previous[1], bounds[1]),
            )
        else:
            raise ValueError(f"unsupported stored source provider: {definition.provider}")

    for key, bounds in requested.items():
        visit(key, *bounds)
    return dict(sorted(ranges.items()))


def latest_acquisition_failure_notes(
    connection: sqlite3.Connection, *, series: Sequence[SeriesDefinition]
) -> dict[str, tuple[str, ...]]:
    """Describe current retained-store acquisition health, not historic value quality.

    A later successful run clears the notice. Missing run metadata is not success.
    Do not copy error messages (which can contain URLs/secrets) into model inputs,
    or attach these later attempts to the original observation's vintage/flags.
    """
    failures = {
        source: (
            f"Latest recorded acquisition in this retained store failed: source={source}, "
            f"run_id={run_id}, finished_at={finished}. This is acquisition status, not "
            "the observation date or a point-in-time assertion; retained values are unchanged."
        )
        for source, run_id, finished in connection.execute(
            "WITH ranked AS (SELECT source_id, run_id, finished_at, status, "
            "ROW_NUMBER() OVER (PARTITION BY source_id "
            "ORDER BY julianday(finished_at) DESC, finished_at DESC, run_id DESC) AS rank "
            "FROM economic_source_runs) "
            "SELECT source_id, run_id, finished_at FROM ranked WHERE rank=1 AND status='failed'"
        )
    }
    definitions = {item.series_id: item for item in series}

    def sources_for(series_id: str) -> set[str]:
        definition = definitions[series_id]
        if definition.provider != "derived":
            return {definition.source_id}
        return set().union(*(sources_for(key) for key in FORMULAS[definition.reading_key].inputs))

    return {
        item.series_id: tuple(
            failures[key] for key in sorted(sources_for(item.series_id)) if key in failures
        )
        for item in series
    }


def build_store_observation_reader(
    connection: sqlite3.Connection,
    *,
    series: Sequence[SeriesDefinition],
    vintage_cutoff: date | None = None,
) -> ObservationReader:
    """Bind one registry-aware observation reader to an open L1 connection.

    ``vintage_cutoff`` separates the source publication-date cutoff from the
    observation range's end. Reading uses its as-of as the cutoff only for providers
    that opt in; other providers retain the latest stored history, including later
    retrievals and revisions. No overall historical-knowledge guarantee follows.
    """

    definitions = {item.series_id: item for item in series}
    options = OptionSeriesReader(connection)

    def read(series_id: str, start: date, end: date) -> tuple[ObservationRecord, ...]:
        definition = definitions.get(series_id)
        if definition is None:
            raise KeyError(f"unknown indicator series: {series_id}")
        if definition.provider == "derived":
            inputs = {
                key: read(key, *bounds)
                for key, bounds in _derived_input_ranges(definition, start, end).items()
            }
            return derive_series(definition, inputs, start=start, end=end)
        if definition.provider in NATIVE_BULK_PROVIDERS:
            return read_native_series(
                connection, definition, start=start, end=end, vintage_cutoff=vintage_cutoff
            )
        if definition.provider == "jquants_options":
            return options.read(definition, start=start, end=end)
        if definition.provider in DOCUMENT_PROVIDERS:
            return read_document_series(connection, definition, start=start, end=end)
        raise ValueError(f"unsupported stored source provider: {definition.provider}")

    return read


__all__ = [
    "build_store_observation_reader",
    "source_record_ranges",
]
