"""Owner-declared physical snapshot schema; persisted identity and field semantics are unchanged."""

from __future__ import annotations

import pyarrow as pa  # type: ignore[import-untyped]

from baibai_engine.storage.snapshots.contracts import LakeColumn, LakeDataset

_TEXT = pa.string()

JQUANTS_MASTER_SNAPSHOTS = LakeDataset(
    name="jquants.master_snapshots",
    layer="l2_processed",
    sqlite_table="jquants_master_snapshots",
    date_column="snapshot_date",
    partition_grain="year",
    contract_version=3,
    coverage_authority="unproven",
    population_column="security_code",
    columns=(
        LakeColumn("snapshot_date", "TEXT", _TEXT, False, 1),
        LakeColumn("security_code", "TEXT", _TEXT, False, 2),
        LakeColumn("record_json", "TEXT", _TEXT, False),
    ),
)
