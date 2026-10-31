"""Owner snapshot contracts: retained history, no claim of source completeness."""

import pyarrow as pa  # type: ignore[import-untyped]

from baibai_engine.storage.snapshots.contracts import LakeColumn, LakeDataset

ARTICLE_PROJECTION_RUNS = LakeDataset(
    name="articles.projection_runs",
    sqlite_table="article_projection_runs",
    date_column="generated_on",
    layer="l2_processed",
    coverage_authority="unproven",
    population_column=None,
    columns=(
        LakeColumn("run_id", "TEXT", pa.string(), False, 1),
        LakeColumn("receipt_id", "TEXT", pa.string(), False, 0),
        LakeColumn("generated_on", "TEXT", pa.string(), False, 0),
        LakeColumn("run_json", "TEXT", pa.string(), False, 0),
    ),
)

ARTICLE_OBSERVATIONS = LakeDataset(
    name="articles.observations",
    sqlite_table="article_observations",
    date_column="received_on",
    layer="l2_processed",
    coverage_authority="unproven",
    population_column=None,
    columns=(
        LakeColumn("observation_id", "TEXT", pa.string(), False, 1),
        LakeColumn("article_id", "TEXT", pa.string(), False, 0),
        LakeColumn("receipt_id", "TEXT", pa.string(), False, 0),
        LakeColumn("received_on", "TEXT", pa.string(), False, 0),
        LakeColumn("observation_sha256", "TEXT", pa.string(), False, 0),
        LakeColumn("observation_json", "TEXT", pa.string(), False, 0),
    ),
)

ARTICLE_EXTRACTION_ARTIFACTS = LakeDataset(
    name="articles.extraction_artifacts",
    sqlite_table="article_extraction_artifacts",
    date_column="generated_on",
    layer="l2_processed",
    coverage_authority="unproven",
    population_column=None,
    columns=(
        LakeColumn("artifact_id", "TEXT", pa.string(), False, 1),
        LakeColumn("observation_id", "TEXT", pa.string(), False, 0),
        LakeColumn("generated_on", "TEXT", pa.string(), False, 0),
        LakeColumn("artifact_sha256", "TEXT", pa.string(), False, 0),
        LakeColumn("artifact_json", "TEXT", pa.string(), False, 0),
    ),
)
