"""Rebuild daily market prices from native L1 pages, without a prior L2 store."""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import date
from typing import Any

import pyarrow as pa  # type: ignore[import-untyped]

from baibai_engine.data_collection.providers.jquants.contracts import (
    JQuantsRequest,
    RetainedRequest,
)
from baibai_engine.data_collection.providers.jquants.day_selection import select_full_days
from baibai_engine.machine_analysis.observations.jquants_decode import JQuantsProviderError
from baibai_engine.machine_analysis.observations.normalization import NormalizedRows
from baibai_engine.machine_analysis.observations.prices.jquants_normalization import (
    normalize_daily_bars,
)
from baibai_engine.machine_analysis.observations.prices.snapshot_contracts import JQUANTS_DAILY_BARS
from baibai_engine.storage.snapshots.models import CoverageStatus

# The old cold Parquet fields were all nullable, independent of the Lake key constraints.
PRICE_BAR_SCHEMA = pa.schema(
    (column.name, column.arrow_type) for column in JQUANTS_DAILY_BARS.columns
)
_PRICE_RECEIPT_SCHEMA = pa.schema(
    [
        ("day", pa.date32()),
        ("request_id", pa.string()),
        ("projection_status", pa.string()),
        ("l1_coverage_status", pa.string()),
        ("rejected_count", pa.int64()),
        ("excluded_count", pa.int64()),
        ("incomplete_requests", pa.int64()),
    ]
)

type PriceReceiptRow = tuple[date, str | None, str, int, int, int]


@dataclass(frozen=True, slots=True)
class PriceDay:
    day: date
    request_id: str | None
    rows: tuple[tuple[Any, ...], ...]
    status: str
    rejected_count: int
    excluded_count: int
    incomplete_requests: int

    def receipt_row(self) -> PriceReceiptRow:
        return (
            self.day,
            self.request_id,
            self.status,
            self.rejected_count,
            self.excluded_count,
            self.incomplete_requests,
        )


@dataclass(slots=True)
class PriceBatchRows:
    """Hold only normalized values per successful receipt in one local application batch."""

    _prepared: dict[str, tuple[JQuantsRequest, NormalizedRows]] = field(default_factory=dict)

    def consume(
        self, identity: str, request: JQuantsRequest, records: list[dict[str, Any]]
    ) -> None:
        if request.source != "daily_bars" or set(request.params) != {"date"}:
            return
        try:
            rows = normalize_daily_bars(records)
        except (JQuantsProviderError, ValueError, TypeError, OverflowError, RecursionError):
            # Only a selected receipt can raise a projection error, after clocks and ties.
            return
        self._prepared[identity] = request, rows

    def get(self, identity: str, request: JQuantsRequest) -> NormalizedRows | None:
        pair = self._prepared.get(identity)
        return pair[1] if pair is not None and pair[0] is request else None


def rebuild_price_days(
    requests: Iterable[RetainedRequest],
    *,
    days: Iterable[date],
    prepared: PriceBatchRows | None = None,
) -> dict[date, PriceDay]:
    wanted = set(days)
    if not wanted:
        return {}
    selected, incomplete = select_full_days(requests, source="daily_bars", days=wanted)
    output: dict[date, PriceDay] = {}
    for day in sorted(wanted):
        chosen = selected.get(day)
        if chosen is None:
            output[day] = PriceDay(day, None, (), "unavailable", 0, 0, incomplete[day])
            continue
        identity, request = chosen
        normalized = None if prepared is None else prepared.get(identity, request)
        if normalized is None:
            normalized = normalize_daily_bars(request.records())
        if any(str(row[1]) != day.isoformat() for row in normalized.rows):
            raise ValueError("daily price payload contains a date outside the request")
        output[day] = PriceDay(
            day,
            identity,
            tuple(normalized.rows),
            normalized.status,
            normalized.rejected_count,
            normalized.excluded_count,
            incomplete[day],
        )
    return output


def _rows_to_batch(rows: Sequence[tuple[Any, ...]], schema: Any) -> Any:
    return pa.RecordBatch.from_arrays(
        [
            pa.array(column, type=field.type)
            for column, field in zip(zip(*rows, strict=True), schema, strict=True)
        ],
        schema=schema,
    )


def price_bars_to_arrow(results: Iterable[PriceDay]) -> Any:
    """Project one bounded input batch without a SQL round trip or inferred null types."""
    return pa.Table.from_batches(
        (_rows_to_batch(item.rows, PRICE_BAR_SCHEMA) for item in results if item.rows),
        schema=PRICE_BAR_SCHEMA,
    )


def price_receipts_to_arrow(
    receipts: Iterable[PriceReceiptRow], *, l1_coverage_status: CoverageStatus
) -> Any:
    rows = [
        (day, request_id, status, l1_coverage_status, rejected, excluded, incomplete)
        for day, request_id, status, rejected, excluded, incomplete in receipts
    ]
    if not rows:
        return _PRICE_RECEIPT_SCHEMA.empty_table()
    return pa.Table.from_batches([_rows_to_batch(rows, _PRICE_RECEIPT_SCHEMA)])
