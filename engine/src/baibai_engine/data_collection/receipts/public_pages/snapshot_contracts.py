"""Owner-declared physical snapshot schema; persisted identity and field semantics are unchanged."""

from __future__ import annotations

import pyarrow as pa  # type: ignore[import-untyped]

from baibai_engine.storage.snapshots.contracts import LakeColumn, LakeDataset

_TEXT = pa.string()

EQUITY_WEB_SOURCE_RESPONSES = LakeDataset(
    name="equity_web.source_responses",
    layer="l1_source",
    sqlite_table="equity_web_source_responses",
    date_column="received_on",
    coverage_authority="unproven",
    population_column=None,
    columns=(
        LakeColumn("receipt_id", "TEXT", _TEXT, False, 1),
        LakeColumn("attempt_id", "TEXT", _TEXT, False),
        LakeColumn("source", "TEXT", _TEXT, False),
        LakeColumn("requested_ticker", "TEXT", _TEXT, False),
        LakeColumn("role", "TEXT", _TEXT, False),
        LakeColumn("parent_receipt_id", "TEXT", _TEXT, True),
        LakeColumn("requested_url", "TEXT", _TEXT, False),
        LakeColumn("received_at_utc", "TEXT", _TEXT, False),
        LakeColumn("received_on", "TEXT", _TEXT, False),
        LakeColumn("selected_encoding", "TEXT", _TEXT, False),
        LakeColumn("body_gzip", "BLOB", pa.binary(), False),
    ),
)
