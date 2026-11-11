"""Read back TradingView originals and the final projection for applied snapshot days."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Iterator, Sequence
from contextlib import closing
from datetime import date
from pathlib import Path
from typing import Any

from baibai_engine.data_collection.providers.tradingview.receipts import read_tool_results
from baibai_engine.data_collection.providers.tradingview.snapshot_contracts import (
    TRADINGVIEW_SOURCE_RECEIPTS,
)
from baibai_engine.machine_analysis.observations.external_assessments.snapshot_contracts import (
    TRADINGVIEW_FORECAST_SNAPSHOTS,
)
from baibai_engine.storage.objects.r2 import LakePublishError, ObjectStore
from baibai_engine.storage.snapshots.contracts import LakeDataset
from baibai_engine.storage.snapshots.duck import LakeSession, lake_session
from baibai_engine.storage.snapshots.objects import LakeObjectCache, LocalMirrorSource
from baibai_engine.storage.snapshots.reader import accepted_dataset, materialize_partitions
from baibai_engine.storage.sqlite.source_cache.read import connect_read_only
from baibai_jobs.publication.snapshot_publication import (
    _fetch,
    _resolve_serving_release,
    _serving_pointer,
)

_DATASETS = (TRADINGVIEW_SOURCE_RECEIPTS, TRADINGVIEW_FORECAST_SNAPSHOTS)


def _digest(rows: Iterable[Sequence[object]]) -> dict[str, int | str]:
    digest = hashlib.sha256()
    count = 0
    for row in rows:
        count += 1
        for value in row:
            payload = json.dumps(value).encode()
            digest.update(len(payload).to_bytes(8, "big"))
            digest.update(payload)
    return {"rows": count, "sha256": digest.hexdigest()}


def capture_publication(
    database: Path, snapshot_dates: Sequence[date], *, receipt_ids: Sequence[str]
) -> dict[str, Any]:
    """Freeze final days, including earlier first-wins rows and their originals."""
    days = sorted({day.isoformat() for day in snapshot_dates})
    if not days:
        raise ValueError("TradingView publication requires applied snapshot days")
    expected = {}
    with closing(connect_read_only(database)) as conn:
        conn.execute("BEGIN")
        if any(
            receipt.snapshot_date.isoformat() not in days
            for receipt in read_tool_results(conn, receipt_ids)
        ):
            raise ValueError("applied TradingView original differs from its snapshot day")
        for dataset in _DATASETS:
            rows = conn.execute(
                f"SELECT {','.join(column.name for column in dataset.columns)} "  # nosec B608
                f"FROM {dataset.sqlite_table} WHERE snapshot_date IN "
                f"({','.join('?' for _ in days)}) ORDER BY {','.join(dataset.primary_key)}",
                days,
            )
            expected[dataset.name] = {"days": days, **_digest(rows)}
    return expected


def _scope_rows(
    session: LakeSession, *, dataset: LakeDataset, paths: Sequence[Path], days: Sequence[str]
) -> Iterator[tuple[object, ...]]:
    if not paths:
        return
    cursor = session.connection.cursor()
    try:
        cursor.execute(
            f"SELECT {','.join(column.name for column in dataset.columns)} "  # nosec B608
            "FROM read_parquet($files) WHERE snapshot_date IN (SELECT unnest($days)) "
            f"ORDER BY {','.join(dataset.primary_key)}",
            {"files": [str(path) for path in paths], "days": list(days)},
        )
        while batch := cursor.fetchmany(4096):
            yield from batch
    finally:
        cursor.close()


def verify_publication(
    store: ObjectStore,
    *,
    expected: dict[str, Any],
    directory: Path,
    generations: dict[str, str],
) -> dict[str, int]:
    """Require this applied closure in the fixed release before consuming stages."""
    if set(expected) != {dataset.name for dataset in _DATASETS}:
        raise ValueError("TradingView publication readback plan is absent or unsupported")
    pointer = _serving_pointer(store).pointer
    if (
        pointer is None
        or pointer.release_id != generations["lake_release_id"]
        or pointer.manifest_sha256 != generations["lake_manifest_sha256"]
    ):
        raise LakePublishError("TradingView readback differs from the fixed publication")
    directory.mkdir(parents=True, exist_ok=True)
    release = _resolve_serving_release(store, directory, pointer)
    cache = LakeObjectCache(root=directory, source=LocalMirrorSource(directory))
    counts = {}
    with lake_session() as session:
        for dataset in _DATASETS:
            scope = expected[dataset.name]
            days = [date.fromisoformat(day) for day in scope["days"]]
            if not days:
                raise ValueError("TradingView readback requires applied snapshot days")
            manifest = release.dataset_manifests.get(dataset.name)
            if manifest is not None:
                accepted_dataset(release, dataset)
            parts = (
                tuple(
                    part
                    for part in manifest.partitions
                    if any(
                        part.values["year"] == day.year
                        and ("month" not in part.values or part.values["month"] == day.month)
                        for day in days
                    )
                )
                if manifest is not None
                else ()
            )
            for part in parts:
                for obj in part.objects:
                    _fetch(store, directory, obj.key, expected_sha256=obj.sha256)
            paths = materialize_partitions(cache, dataset=dataset, partitions=parts)
            actual = _digest(_scope_rows(session, dataset=dataset, paths=paths, days=scope["days"]))
            if actual != {"rows": scope["rows"], "sha256": scope["sha256"]}:
                raise LakePublishError(
                    "TradingView original/projection publication readback differs"
                )
            counts[dataset.name] = int(actual["rows"])
    return counts
