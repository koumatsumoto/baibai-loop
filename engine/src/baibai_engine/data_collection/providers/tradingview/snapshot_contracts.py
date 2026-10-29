"""Owner-declared physical snapshot schema; persisted identity and field semantics are unchanged."""

from __future__ import annotations

import pyarrow as pa  # type: ignore[import-untyped]

from baibai_engine.storage.snapshots.contracts import LakeColumn, LakeDataset

_TEXT = pa.string()

TRADINGVIEW_SOURCE_RECEIPTS = LakeDataset(
    name="tradingview.source_receipts",
    layer="l1_source",
    sqlite_table="tradingview_source_receipts",
    date_column="snapshot_date",
    partition_grain="year",
    coverage_authority="unproven",
    population_column=None,
    columns=(
        LakeColumn("receipt_id", "TEXT", _TEXT, False, 1),
        LakeColumn("snapshot_date", "TEXT", _TEXT, False),
        LakeColumn("master_asof", "TEXT", _TEXT, False),
        LakeColumn("tool_name", "TEXT", _TEXT, False),
        LakeColumn("received_at_utc", "TEXT", _TEXT, False),
        LakeColumn("requested_symbols_json", "TEXT", _TEXT, False),
        LakeColumn("requested_columns_json", "TEXT", _TEXT, False),
        LakeColumn("result_json", "TEXT", _TEXT, False),
    ),
)
