"""Owner-declared physical snapshot schema; persisted identity and field semantics are unchanged."""

from __future__ import annotations

import pyarrow as pa  # type: ignore[import-untyped]

from baibai_engine.storage.snapshots.contracts import LakeColumn, LakeDataset

_TEXT = pa.string()
_INTEGER = pa.int64()

JQUANTS_SOURCE_PAGES = LakeDataset(
    name="jquants.source_pages",
    layer="l1_source",
    sqlite_table="jquants_source_pages",
    date_column="received_on",
    coverage_authority="unproven",
    population_column=None,
    columns=(
        LakeColumn("request_id", "TEXT", _TEXT, False, 1),
        LakeColumn("page_index", "INTEGER", _INTEGER, False, 2),
        LakeColumn("source", "TEXT", _TEXT, False),
        LakeColumn("query_json", "TEXT", _TEXT, False),
        LakeColumn("received_at_utc", "TEXT", _TEXT, False),
        LakeColumn("received_on", "TEXT", _TEXT, False),
        LakeColumn("body_gzip", "BLOB", pa.binary(), False),
    ),
)
