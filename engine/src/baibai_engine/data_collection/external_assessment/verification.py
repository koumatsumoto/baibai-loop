"""Read-only verification of staged receipts and affected external assessment history."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from contextlib import closing
from pathlib import Path
from typing import cast

from pydantic import ValidationError

from baibai_engine.machine_analysis.external_assessment.observations.contract import (
    ANALYST_FORECAST_COLUMNS,
    ANALYST_FORECAST_TABLE,
    ANALYST_RATING_COLUMNS,
    ANALYST_RATING_TABLE,
    MODEL_VALUATION_COLUMNS,
    MODEL_VALUATION_TABLE,
    QUANTS_SCORE_COLUMNS,
    QUANTS_SCORE_TABLE,
)
from baibai_engine.machine_analysis.external_assessment.observations.models import (
    AnalystForecastObservation,
    AnalystRatingObservation,
    ModelValuationObservation,
    Observation,
    QuantsScoreObservation,
)
from baibai_engine.machine_analysis.external_assessment.observations.rows import observation_row
from baibai_engine.storage.lake.datasets import (
    EXTERNAL_ASSESSMENT_ANALYST_FORECAST_OBSERVATIONS,
    EXTERNAL_ASSESSMENT_ANALYST_RATING_OBSERVATIONS,
    EXTERNAL_ASSESSMENT_MODEL_VALUATION_OBSERVATIONS,
    EXTERNAL_ASSESSMENT_QUANTS_SCORE_OBSERVATIONS,
)
from baibai_engine.storage.lake.duck import LakeSession
from baibai_engine.storage.lake.objects import LakeObjectCache
from baibai_engine.storage.lake.reader import (
    FixedRelease,
    iter_partition_rows,
    materialize_partitions,
)
from baibai_engine.storage.observations.read import connect_read_only


def _row_bytes(row: Sequence[object]) -> bytes:
    return json.dumps(tuple(row), allow_nan=False, separators=(",", ":")).encode() + b"\n"


def verify_observations(database: Path, observations: Sequence[Observation]) -> None:
    # Acquisition checks its exact immutable receipt, not an analytical latest selector.
    with closing(connect_read_only(database)) as connection:
        connection.execute("BEGIN")
        for item in observations:
            table, columns, _ = observation_row(item)
            names = ",".join(columns)
            row = connection.execute(
                f"SELECT {names} FROM {table} WHERE ticker=? AND fetched_at_utc=?",  # nosec B608
                (item.ticker, item.fetched_at_utc.isoformat(timespec="microseconds")),
            ).fetchone()
            if row is None or type(item).model_validate(dict(row)) != item:
                raise ValueError("external assessment receipt readback mismatch")


def history_digests(
    database: Path, *, partitions: set[tuple[str, int, int]] | None = None
) -> dict[str, str]:
    result = {}
    with closing(connect_read_only(database)) as connection:
        connection.execute("BEGIN")
        for dataset, table, columns in (
            (
                EXTERNAL_ASSESSMENT_ANALYST_FORECAST_OBSERVATIONS,
                ANALYST_FORECAST_TABLE,
                ANALYST_FORECAST_COLUMNS,
            ),
            (
                EXTERNAL_ASSESSMENT_MODEL_VALUATION_OBSERVATIONS,
                MODEL_VALUATION_TABLE,
                MODEL_VALUATION_COLUMNS,
            ),
            (
                EXTERNAL_ASSESSMENT_ANALYST_RATING_OBSERVATIONS,
                ANALYST_RATING_TABLE,
                ANALYST_RATING_COLUMNS,
            ),
            (
                EXTERNAL_ASSESSMENT_QUANTS_SCORE_OBSERVATIONS,
                QUANTS_SCORE_TABLE,
                QUANTS_SCORE_COLUMNS,
            ),
        ):
            digest = hashlib.sha256()
            names = ",".join(column[0] for column in columns)
            where = ""
            bounds: list[str] = []
            if partitions is not None:
                months = sorted(
                    (year, month) for name, year, month in partitions if name == dataset.name
                )
                where = " WHERE " + (
                    " OR ".join("(snapshot_date >= ? AND snapshot_date < ?)" for _ in months) or "0"
                )
                for year, month in months:
                    bounds.extend(
                        (
                            f"{year:04}-{month:02}-01",
                            f"{year + (month == 12):04}-{month % 12 + 1:02}-01",
                        )
                    )
            for row in connection.execute(
                f"SELECT {names} FROM {table}{where} ORDER BY ticker,fetched_at_utc",  # nosec B608
                bounds,
            ):
                digest.update(_row_bytes(tuple(row)))
            result[table] = digest.hexdigest()
    return result


def verify_release_history(
    session: LakeSession,
    *,
    release: FixedRelease,
    cache: LakeObjectCache,
    expected_history: Mapping[str, str],
    observations: Sequence[Observation],
    partitions: set[tuple[str, int, int]] | None = None,
) -> None:
    """Verify selected months in primary-key order, including their older receipts."""
    actual_history = {}
    for dataset, model in (
        (EXTERNAL_ASSESSMENT_ANALYST_FORECAST_OBSERVATIONS, AnalystForecastObservation),
        (EXTERNAL_ASSESSMENT_MODEL_VALUATION_OBSERVATIONS, ModelValuationObservation),
        (EXTERNAL_ASSESSMENT_ANALYST_RATING_OBSERVATIONS, AnalystRatingObservation),
        (EXTERNAL_ASSESSMENT_QUANTS_SCORE_OBSERVATIONS, QuantsScoreObservation),
    ):
        expected = {
            (item.ticker, item.fetched_at_utc.isoformat(timespec="microseconds")): item
            for item in observations
            if isinstance(item, model)
        }
        found = set()
        names = tuple(column.name for column in dataset.columns)
        digest = hashlib.sha256()
        manifest = release.dataset_manifests.get(dataset.name)
        paths = (
            materialize_partitions(
                cache,
                dataset=dataset,
                partitions=tuple(
                    part
                    for part in manifest.partitions
                    if partitions is None
                    or (dataset.name, part.values["year"], part.values["month"]) in partitions
                ),
            )
            if manifest is not None
            else ()
        )
        for batch in iter_partition_rows(session, dataset=dataset, paths=paths, columns=names):
            for row in batch:
                digest.update(_row_bytes(row))
                key = (cast("str", row[0]), cast("str", row[1]))
                if key in expected:
                    try:
                        actual = model.model_validate(dict(zip(names, row, strict=True)))
                    except ValidationError:
                        raise ValueError("external assessment receipt readback mismatch") from None
                    if actual != expected[key]:
                        raise ValueError("external assessment receipt readback mismatch")
                    found.add(key)
        if found != set(expected):
            raise ValueError("external assessment receipt readback mismatch")
        actual_history[dataset.sqlite_table] = digest.hexdigest()
    if actual_history != dict(expected_history):
        raise ValueError("external assessment publication changed received history")
