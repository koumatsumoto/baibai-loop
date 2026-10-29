"""Generic physical snapshot descriptors. Concrete dataset meaning belongs to its owner."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Literal

import pyarrow as pa  # type: ignore[import-untyped]

from baibai_engine.storage.snapshots.keys import LakeLayer

PartitionGrain = Literal["year", "month"]
"""How much of one dataset's history a single partition covers.

The grain is declared per dataset rather than derived from how many rows the store
happens to hold. A reader checks the manifest's layout against this contract, so a
layout that moved with the data would start refusing releases on the day a table grew.
Row counts are the evidence for choosing a grain; they are not the mechanism.
"""

_GRAIN_LAYOUTS: dict[PartitionGrain, tuple[str, ...]] = {
    "month": ("year", "month"),
    "year": ("year",),
}

CoverageAuthority = Literal["source_coverage", "unproven"]
"""The dataset-specific evidence used for the operational coverage status.

An absent observation cannot distinguish "not published" from "not fetched", so
range coverage comes from successful fetch records. This also permits genuinely
empty daily-bars responses without treating unfetched days as market holidays.
Reported row/population counts describe retained data, not a completeness proof.

`source_coverage` holds two shapes of record and only one of them can answer the
question. A range claim says a window was fetched, so a span is checkable. A
per-observation claim says which dates carried an observation — measured on the real
store, weekly margin holds 523 single-day intervals and master snapshots 126 — and no
merge of those spans a range, because the days between them are days the source never
published. Proving completeness there would mean knowing the source's publication
calendar, which the store does not hold. Those sources are `unproven`: they can say what
they hold, not that they hold everything.
"""


@dataclass(frozen=True)
class LakeColumn:
    name: str
    sqlite_type: str
    arrow_type: object
    nullable: bool
    primary_key_ordinal: int = 0
    sqlite_default: str | None = None


@dataclass(frozen=True)
class LakeDataset:
    name: str
    sqlite_table: str
    date_column: str
    columns: tuple[LakeColumn, ...]
    layer: LakeLayer
    contract_version: int = 1
    partition_grain: PartitionGrain = "month"
    coverage_authority: CoverageAuthority = "source_coverage"
    coverage_source: str | None = None
    """The `source_coverage.source` that records this dataset's fetches.

    Usually the SQLite table name, but the two are independent identifiers and have
    already drifted apart once, so the exception is declared rather than assumed.
    """
    population_column: str | None = "ticker"
    """The column whose distinct values are the dataset's population, if it has one."""

    @property
    def has_binary_payload(self) -> bool:
        return any(column.sqlite_type == "BLOB" for column in self.columns)

    @property
    def coverage_source_name(self) -> str:
        return self.coverage_source or self.sqlite_table

    @property
    def partition_by(self) -> tuple[str, ...]:
        return _GRAIN_LAYOUTS[self.partition_grain]

    @property
    def primary_key(self) -> tuple[str, ...]:
        return tuple(
            column.name
            for column in sorted(
                self.columns,
                key=lambda item: item.primary_key_ordinal or len(self.columns) + 1,
            )
            if column.primary_key_ordinal
        )

    @property
    def arrow_schema(self) -> object:
        metadata = {
            b"baibai.contract_version": str(self.contract_version).encode(),
            b"baibai.dataset": self.name.encode(),
            b"baibai.layer": self.layer.encode(),
            b"baibai.primary_key": ",".join(self.primary_key).encode(),
            # Existing wire tag for SQLite snapshot ingestion, not a schema version.
            b"baibai.source_kind": b"legacy_sqlite_import",
        }
        return pa.schema(
            [
                pa.field(column.name, column.arrow_type, nullable=column.nullable)
                for column in self.columns
            ],
            metadata=metadata,
        )


Period = tuple[int, ...]
"""One partition's place in the calendar, ordered as the dataset's layout names it."""


def period_values(dataset: LakeDataset, period: Period) -> dict[str, int]:
    return dict(zip(dataset.partition_by, period, strict=True))


def period_label(period: Period) -> str:
    return "-".join((f"{period[0]:04d}", *(f"{part:02d}" for part in period[1:])))


def freeze_catalog(catalog: Mapping[str, LakeDataset]) -> Mapping[str, LakeDataset]:
    """Fix the caller's complete table ownership inventory for one operation."""
    fixed = dict(catalog)
    if any(name != descriptor.name for name, descriptor in fixed.items()):
        raise ValueError("dataset catalog key differs from its descriptor identity")
    tables = [descriptor.sqlite_table for descriptor in fixed.values()]
    if len(set(tables)) != len(tables):
        raise ValueError("dataset catalog maps multiple owners to the same SQLite table")
    return MappingProxyType(fixed)
