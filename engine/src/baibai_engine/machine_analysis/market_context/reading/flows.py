"""Select published investor-flow revisions before interpreting native balances."""

from __future__ import annotations

import sqlite3
from collections.abc import Mapping
from dataclasses import replace
from datetime import UTC, date, datetime
from math import isfinite
from typing import cast

from baibai_engine.data_collection.economic.records import read_snapshots
from baibai_engine.machine_analysis.market_context.indicators.definitions import SeriesDefinition
from baibai_engine.machine_analysis.market_context.indicators.observations import ObservationRecord

from .observations import reading_observation


def read_flow_series(
    connection: sqlite3.Connection,
    series: SeriesDefinition,
    *,
    start: date,
    end: date,
    publication_cutoff: date,
) -> tuple[ObservationRecord, ...]:
    if series.reading_key != "foreigners_net_value" or series.unit != "jpy-thousand":
        raise ValueError(f"investor flow series {series.series_id} has inconsistent unit")
    selected: dict[date, tuple[date, list[Mapping[str, object]]]] = {}
    # Source snapshots are publication-day responses, not aggregation-week snapshots.
    # A later correction may describe a period earlier than the requested range end.
    for snapshot in read_snapshots(
        connection, date.min, publication_cutoff, source_id=series.source_id
    ):
        for row in snapshot.records:
            period_end = _date(row.get("EnDate"), "EnDate")
            if not start <= period_end <= end:
                continue
            current = selected.get(period_end)
            if current is None or current[0] < snapshot.observed_at:
                selected[period_end] = (snapshot.observed_at, [row])
            else:
                current[1].append(row)
    observations = []
    for period_end, (published, rows) in sorted(selected.items()):
        period_starts = {
            None if row.get("StDate") in (None, "") else _date(row["StDate"], "StDate")
            for row in rows
        }
        if len(period_starts) != 1:
            raise ValueError(f"conflicting aggregation periods for {series.series_id}")
        period_start = period_starts.pop()
        if (period_start is not None and period_start > period_end) or period_end > published:
            raise ValueError(f"invalid aggregation period for {series.series_id}")
        values = {_balance(row.get("FrgnBal")) for row in rows}
        # Select the publication first. An absent balance does not revive an older one
        # and is not reconstructed from purchases and sales with unconfirmed semantics.
        if None in values:
            continue
        if len(values) != 1:
            raise ValueError(f"conflicting published balances for {series.series_id}")
        value = cast(float, values.pop())
        observations.append(
            replace(
                reading_observation(
                    series,
                    observed_at=period_end,
                    value=value,
                    vintage_at=datetime(published.year, published.month, published.day, tzinfo=UTC),
                ),
                period_start=period_start,
            )
        )
    return tuple(observations)


def _date(raw: object, field: str) -> date:
    if not isinstance(raw, str):
        raise ValueError(f"investor flow has no native {field}")
    return date.fromisoformat(raw)


def _balance(raw: object) -> float | None:
    if isinstance(raw, bool) or not isinstance(raw, (int, float, str)):
        return None
    try:
        value = float(raw)
    except (ValueError, OverflowError):
        return None
    return value if isfinite(value) else None
