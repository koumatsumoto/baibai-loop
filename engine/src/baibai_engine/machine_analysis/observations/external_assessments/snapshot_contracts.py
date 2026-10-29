"""Owner-declared physical snapshot schema; persisted identity and field semantics are unchanged."""

from __future__ import annotations

import pyarrow as pa  # type: ignore[import-untyped]

from baibai_engine.machine_analysis.observations.external_assessments.contract import (
    ANALYST_FORECAST_COLUMNS,
    ANALYST_FORECAST_TABLE,
    ANALYST_RATING_COLUMNS,
    ANALYST_RATING_TABLE,
    MODEL_VALUATION_COLUMNS,
    MODEL_VALUATION_TABLE,
    QUANTS_SCORE_COLUMNS,
    QUANTS_SCORE_TABLE,
)
from baibai_engine.machine_analysis.observations.external_assessments.identity import (
    ANALYST_FORECAST_DATASET,
    ANALYST_RATING_DATASET,
    MODEL_VALUATION_DATASET,
    QUANTS_SCORE_DATASET,
)
from baibai_engine.machine_analysis.observations.external_assessments.tradingview.contract import (
    COLUMNS as TV_COLUMNS,
)
from baibai_engine.machine_analysis.observations.external_assessments.tradingview.contract import (
    TABLE as TV_TABLE,
)
from baibai_engine.storage.snapshots.contracts import LakeColumn, LakeDataset

_TEXT = pa.string()
_REAL = pa.float64()
_INTEGER = pa.int64()

TRADINGVIEW_FORECAST_SNAPSHOTS = LakeDataset(
    name="tradingview.forecast_snapshots",
    layer="l2_processed",
    sqlite_table=TV_TABLE,
    date_column="snapshot_date",
    coverage_authority="unproven",
    columns=tuple(
        LakeColumn(
            name, kind, {"TEXT": _TEXT, "REAL": _REAL, "INTEGER": _INTEGER}[kind], nullable, pk
        )
        for name, kind, nullable, pk in TV_COLUMNS
    ),
)

EXTERNAL_ASSESSMENT_MODEL_VALUATION_OBSERVATIONS = LakeDataset(
    name=MODEL_VALUATION_DATASET,
    layer="l2_processed",
    sqlite_table=MODEL_VALUATION_TABLE,
    date_column="snapshot_date",
    coverage_authority="unproven",
    columns=tuple(
        LakeColumn(
            name, kind, {"TEXT": _TEXT, "REAL": _REAL, "INTEGER": _INTEGER}[kind], nullable, pk
        )
        for name, kind, nullable, pk in MODEL_VALUATION_COLUMNS
    ),
)

EXTERNAL_ASSESSMENT_ANALYST_RATING_OBSERVATIONS = LakeDataset(
    name=ANALYST_RATING_DATASET,
    layer="l2_processed",
    sqlite_table=ANALYST_RATING_TABLE,
    date_column="snapshot_date",
    coverage_authority="unproven",
    columns=tuple(
        LakeColumn(
            name, kind, {"TEXT": _TEXT, "REAL": _REAL, "INTEGER": _INTEGER}[kind], nullable, pk
        )
        for name, kind, nullable, pk in ANALYST_RATING_COLUMNS
    ),
)

EXTERNAL_ASSESSMENT_QUANTS_SCORE_OBSERVATIONS = LakeDataset(
    name=QUANTS_SCORE_DATASET,
    layer="l2_processed",
    sqlite_table=QUANTS_SCORE_TABLE,
    date_column="snapshot_date",
    coverage_authority="unproven",
    columns=tuple(
        LakeColumn(
            name, kind, {"TEXT": _TEXT, "REAL": _REAL, "INTEGER": _INTEGER}[kind], nullable, pk
        )
        for name, kind, nullable, pk in QUANTS_SCORE_COLUMNS
    ),
)

EXTERNAL_ASSESSMENT_ANALYST_FORECAST_OBSERVATIONS = LakeDataset(
    name=ANALYST_FORECAST_DATASET,
    layer="l2_processed",
    sqlite_table=ANALYST_FORECAST_TABLE,
    date_column="snapshot_date",
    coverage_authority="unproven",
    columns=tuple(
        LakeColumn(
            name, kind, {"TEXT": _TEXT, "REAL": _REAL, "INTEGER": _INTEGER}[kind], nullable, pk
        )
        for name, kind, nullable, pk in ANALYST_FORECAST_COLUMNS
    ),
)
