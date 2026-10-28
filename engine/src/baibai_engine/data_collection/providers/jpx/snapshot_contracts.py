"""Owner-declared physical snapshot schema; persisted identity and field semantics are unchanged."""

from __future__ import annotations

import pyarrow as pa  # type: ignore[import-untyped]

from baibai_engine.storage.snapshots.contracts import LakeColumn, LakeDataset

_TEXT = pa.string()

JPX_SOURCE_BUNDLES = LakeDataset(
    name="jpx.source_bundles",
    layer="l1_source",
    sqlite_table="jpx_source_bundles",
    date_column="started_on",
    coverage_authority="unproven",
    population_column=None,
    columns=(
        LakeColumn("bundle_id", "TEXT", _TEXT, False, 1),
        LakeColumn("collection_name", "TEXT", _TEXT, False),
        LakeColumn("asof_date", "TEXT", _TEXT, False),
        LakeColumn("started_at_utc", "TEXT", _TEXT, False),
        LakeColumn("started_on", "TEXT", _TEXT, False),
        LakeColumn("expected_resources_json", "TEXT", _TEXT, True),
        LakeColumn("completed_at_utc", "TEXT", _TEXT, True),
    ),
)

JPX_SOURCE_RESPONSES = LakeDataset(
    name="jpx.source_responses",
    layer="l1_source",
    sqlite_table="jpx_source_responses",
    date_column="received_on",
    coverage_authority="unproven",
    population_column=None,
    columns=(
        LakeColumn("bundle_id", "TEXT", _TEXT, False, 1),
        LakeColumn("resource_name", "TEXT", _TEXT, False, 2),
        LakeColumn("requested_url", "TEXT", _TEXT, False),
        LakeColumn("received_at_utc", "TEXT", _TEXT, False),
        LakeColumn("received_on", "TEXT", _TEXT, False),
        LakeColumn("content_type", "TEXT", _TEXT, False),
        LakeColumn("body_gzip", "BLOB", pa.binary(), False),
    ),
)
