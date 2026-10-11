"""Read-only verification of staged receipts and complete retail history."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from contextlib import closing
from pathlib import Path
from typing import cast

from pydantic import ValidationError

from baibai_engine.market.lake.datasets import (
    RETAIL_ANALYST_RATING_OBSERVATIONS,
    RETAIL_MODEL_VALUATION_OBSERVATIONS,
)
from baibai_engine.market.lake.duck import LakeSession
from baibai_engine.market.lake.objects import LakeObjectCache
from baibai_engine.market.lake.reader import (
    FixedRelease,
    iter_partition_rows,
    materialize_partitions,
)
from baibai_engine.market.sqlite.read import connect_read_only

from .contract import (
    ANALYST_RATING_COLUMNS,
    ANALYST_RATING_TABLE,
    MODEL_VALUATION_COLUMNS,
    MODEL_VALUATION_TABLE,
)
from .models import AnalystRatingObservation, ModelValuationObservation, Observation
from .reader import read_observations


def _row_bytes(row: Sequence[object]) -> bytes:
    return json.dumps(tuple(row), allow_nan=False, separators=(",", ":")).encode() + b"\n"


def verify_observations(database: Path, observations: Sequence[Observation]) -> None:
    for item in observations:
        history = read_observations(database, item.ticker, item.fetched_at_utc)
        actual = (
            history.model_valuation.latest
            if isinstance(item, ModelValuationObservation)
            else history.analyst_rating.latest
        )
        if actual != item:
            raise ValueError("retail receipt readback mismatch")


def history_digests(database: Path) -> dict[str, str]:
    result = {}
    with closing(connect_read_only(database)) as connection:
        connection.execute("BEGIN")
        for table, columns in (
            (MODEL_VALUATION_TABLE, MODEL_VALUATION_COLUMNS),
            (ANALYST_RATING_TABLE, ANALYST_RATING_COLUMNS),
        ):
            digest = hashlib.sha256()
            names = ",".join(column[0] for column in columns)
            for row in connection.execute(
                f"SELECT {names} FROM {table} ORDER BY ticker,fetched_at_utc"  # nosec B608
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
) -> None:
    """Verify only the fixed release's retail history; no partial SQLite hydrate.

    Scan all months together in primary-key order, retaining one bounded reader
    batch and the current stage's receipt keys rather than a full history list.
    """
    actual_history = {}
    for dataset, model in (
        (RETAIL_MODEL_VALUATION_OBSERVATIONS, ModelValuationObservation),
        (RETAIL_ANALYST_RATING_OBSERVATIONS, AnalystRatingObservation),
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
            materialize_partitions(cache, dataset=dataset, partitions=manifest.partitions)
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
                        raise ValueError("retail receipt readback mismatch") from None
                    if actual != expected[key]:
                        raise ValueError("retail receipt readback mismatch")
                    found.add(key)
        if found != set(expected):
            raise ValueError("retail receipt readback mismatch")
        actual_history[dataset.sqlite_table] = digest.hexdigest()
    if actual_history != dict(expected_history):
        raise ValueError("retail publication changed received history")
