"""Owner snapshot contracts: retained history, no claim of source completeness."""

import pyarrow as pa  # type: ignore[import-untyped]

from baibai_engine.storage.snapshots.contracts import LakeColumn, LakeDataset

ARTICLE_RECEIPTS = LakeDataset(
    name="articles.source_receipts",
    sqlite_table="article_source_receipts",
    date_column="received_on",
    layer="l1_source",
    coverage_authority="unproven",
    population_column=None,
    columns=(
        LakeColumn("receipt_id", "TEXT", pa.string(), False, 1),
        LakeColumn("source", "TEXT", pa.string(), False, 0),
        LakeColumn("received_on", "TEXT", pa.string(), False, 0),
        LakeColumn("receipt_sha256", "TEXT", pa.string(), False, 0),
        LakeColumn("receipt_json", "TEXT", pa.string(), False, 0),
        LakeColumn("body_gzip", "BLOB", pa.binary(), True, 0),
    ),
)
