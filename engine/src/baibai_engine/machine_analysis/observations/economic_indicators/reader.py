"""Selected L1 observations and transient L2 values from the same input snapshot.

Only providers whose spec declares publication-date vintages are clamped to
the requested cutoff. This does not establish exact historical API availability.
Most bulk history stores the acquisition time as its vintage, not the original
publication time; clamping those providers would make
valid history disappear from snapshots whose as-of predates the backfill.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Sequence
from datetime import date

from baibai_engine.data_collection.catalog import DOCUMENT_PROVIDERS
from baibai_engine.machine_analysis.observations.economic_indicators.contracts import (
    SeriesDefinition,
)
from baibai_engine.machine_analysis.observations.economic_indicators.derived import derive_series
from baibai_engine.machine_analysis.observations.economic_indicators.documents import (
    read_document_series,
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
)


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
            formula = FORMULAS[definition.reading_key]
            input_start = start.replace(day=1) if formula.alignment == "monthly" else start
            inputs = {key: read(key, input_start, end) for key in formula.inputs}
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
]
