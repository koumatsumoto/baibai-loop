"""Owner-declared physical snapshot schema; persisted identity and field semantics are unchanged."""

from __future__ import annotations

import pyarrow as pa  # type: ignore[import-untyped]

from baibai_engine.storage.snapshots.contracts import LakeColumn, LakeDataset

_TEXT = pa.string()
_INTEGER = pa.int64()

EDINET_LIST_RESPONSES = LakeDataset(
    name="edinet.list_responses",
    layer="l1_source",
    sqlite_table="edinet_list_responses",
    date_column="doc_date",
    partition_grain="year",
    coverage_authority="unproven",
    population_column=None,
    columns=(
        LakeColumn("receipt_id", "TEXT", _TEXT, False, 1),
        LakeColumn("doc_date", "TEXT", _TEXT, False),
        LakeColumn("received_at_utc", "TEXT", _TEXT, False),
        LakeColumn("body_gzip", "BLOB", pa.binary(), False),
    ),
)

EDINET_SOURCE_ARCHIVES = LakeDataset(
    name="edinet.source_archives",
    layer="l1_source",
    sqlite_table="edinet_source_archives",
    date_column="stored_on",
    coverage_authority="unproven",
    population_column=None,
    columns=(
        LakeColumn("doc_id", "TEXT", _TEXT, False, 1),
        LakeColumn("source_type", "INTEGER", _INTEGER, False, 2),
        LakeColumn("stored_on", "TEXT", _TEXT, False),
        LakeColumn("zip_content", "BLOB", pa.binary(), False),
    ),
)
