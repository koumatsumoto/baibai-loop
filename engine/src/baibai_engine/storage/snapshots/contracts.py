"""Generic physical snapshot descriptors. Concrete dataset meaning belongs to its owner."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Literal

import pyarrow as pa  # type: ignore[import-untyped]

from baibai_engine.storage.snapshots.keys import LakeLayer

PROJECTION_REVISION_METADATA_KEY = b"baibai.projection_revision"

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
"""Range fetch evidence can prove coverage; sparse observations alone cannot."""


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
    object_date_grain: Literal["day", "month"] | None = None
    """Optional physical split inside each unchanged logical partition."""
    coverage_authority: CoverageAuthority = "source_coverage"
    coverage_source: str | None = None
    """The `source_coverage.source` that records this dataset's fetches.

    Usually the SQLite table name, but the two are independent identifiers and have
    already drifted apart once, so the exception is declared rather than assumed.
    """
    population_column: str | None = "ticker"
    """The column whose distinct values are the dataset's population, if it has one."""

    legacy_read_contracts: tuple[LakeDataset, ...] = ()
    """Exact owner-declared historical schemas; never used to publish new objects."""
    legacy_read_defaults: tuple[tuple[str, str], ...] = ()
    """Owner-declared values for fields absent from historical rows."""

    def read_contract(self, version: int) -> LakeDataset:
        if version == self.contract_version:
            return self
        if not self.legacy_read_contracts:
            raise ValueError(
                f"dataset {self.name} publishes contract v{version}; "
                f"this reader accepts only v{self.contract_version}"
            )
        matches = [item for item in self.legacy_read_contracts if item.contract_version == version]
        if len(matches) != 1:
            raise ValueError(f"dataset {self.name} has no declared read contract v{version}")
        selected = matches[0]
        if selected.name != self.name or selected.layer != self.layer:
            raise ValueError("historical read contract differs from its owner identity")
        return selected

    def project_read_row(self, source: LakeDataset, row: tuple[object, ...]) -> tuple[object, ...]:
        """Project a verified historical row using only explicit owner defaults."""
        if source != self.read_contract(source.contract_version):
            raise ValueError("row source differs from its declared read contract")
        source_names = tuple(column.name for column in source.columns)
        target_names = tuple(column.name for column in self.columns)
        values = dict(zip(source_names, row, strict=True))
        if source == self:
            return row
        defaults = dict(self.legacy_read_defaults)
        missing = set(target_names) - set(source_names)
        if set(source_names) - set(target_names) or missing != defaults.keys():
            raise ValueError("historical row projection differs from declared owner defaults")
        return tuple(values[name] if name in values else defaults[name] for name in target_names)

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

    def arrow_schema_for(
        self, source_kind: Literal["legacy_sqlite_import", "l1_derived"]
    ) -> object:
        metadata = {
            b"baibai.contract_version": str(self.contract_version).encode(),
            b"baibai.dataset": self.name.encode(),
            b"baibai.layer": self.layer.encode(),
            b"baibai.primary_key": ",".join(self.primary_key).encode(),
            b"baibai.source_kind": source_kind.encode(),
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
