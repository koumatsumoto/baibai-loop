"""Read back applied JPX originals and the projection scopes they replaced."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Iterator, Mapping, Sequence
from contextlib import closing
from datetime import date
from pathlib import Path
from typing import Any

from baibai_engine.data_collection.providers.jpx.snapshot_contracts import (
    JPX_SOURCE_BUNDLES,
    JPX_SOURCE_RESPONSES,
)
from baibai_engine.machine_analysis.observations.disclosures.snapshot_contracts import (
    JPX_EARNINGS_CALENDAR,
    JPX_REGULATION_FLAGS,
    JPX_REGULATION_SOURCES,
)
from baibai_engine.storage.objects.r2 import LakePublishError, ObjectStore
from baibai_engine.storage.snapshots.contracts import LakeDataset
from baibai_engine.storage.snapshots.duck import LakeSession, lake_session
from baibai_engine.storage.snapshots.objects import LakeObjectCache, LocalMirrorSource
from baibai_engine.storage.snapshots.reader import (
    accepted_dataset,
    materialize_partitions,
)
from baibai_engine.storage.sqlite.source_cache.read import connect_read_only
from baibai_jobs.publication.snapshot_publication import (
    _fetch,
    _resolve_serving_release,
    _serving_pointer,
)

_DATASETS = {
    dataset.name: dataset
    for dataset in (
        JPX_SOURCE_BUNDLES,
        JPX_SOURCE_RESPONSES,
        JPX_EARNINGS_CALENDAR,
        JPX_REGULATION_FLAGS,
        JPX_REGULATION_SOURCES,
    )
}


def _digest(rows: Iterable[Sequence[object]]) -> dict[str, int | str]:
    digest = hashlib.sha256()
    count = 0
    for row in rows:
        count += 1
        for value in row:
            payload = value if isinstance(value, bytes) else json.dumps(value).encode()
            digest.update(len(payload).to_bytes(8, "big"))
            digest.update(payload)
    return {"rows": count, "sha256": digest.hexdigest()}


def capture_publication(database: Path, bundle_ids: Sequence[str]) -> dict[str, Any]:
    """Freeze applied originals and the final replaced projection scopes."""
    scopes: dict[str, dict[str, Any]] = {}
    with closing(connect_read_only(database)) as conn:
        conn.execute("BEGIN")
        bundles = []
        for bundle_id in sorted(set(bundle_ids)):
            row = conn.execute(
                "SELECT collection_name, asof_date, started_on "
                "FROM jpx_source_bundles WHERE bundle_id=?",
                (bundle_id,),
            ).fetchone()
            if row is None:
                raise ValueError("applied JPX original bundle is missing")
            bundles.append(row)
        if not bundles:
            raise ValueError("JPX publication requires applied original bundles")
        for dataset in (JPX_SOURCE_BUNDLES, JPX_SOURCE_RESPONSES):
            days = (
                [row[2] for row in bundles]
                if dataset == JPX_SOURCE_BUNDLES
                else [
                    row[0]
                    for bundle_id in sorted(set(bundle_ids))
                    for row in conn.execute(
                        "SELECT received_on FROM jpx_source_responses WHERE bundle_id=?",
                        (bundle_id,),
                    )
                ]
            )
            scopes[dataset.name] = {
                "bundle_ids": sorted(set(bundle_ids)),
                "months": sorted({day[:7] for day in days}),
            }
        if any(row[0] == "earnings_calendar" for row in bundles):
            scopes[JPX_EARNINGS_CALENDAR.name] = {}
        regulation_days = sorted({row[1] for row in bundles if row[0] == "regulations"})
        if regulation_days:
            for dataset in (JPX_REGULATION_FLAGS, JPX_REGULATION_SOURCES):
                scopes[dataset.name] = {"days": regulation_days}
        for name, scope in scopes.items():
            dataset = _DATASETS[name]
            names = tuple(column.name for column in dataset.columns)
            bounds = scope.get("bundle_ids", scope.get("days"))
            column = "bundle_id" if "bundle_ids" in scope else dataset.date_column
            where = (
                " WHERE " + column + " IN (" + ",".join("?" for _ in bounds) + ")"
                if bounds
                else " WHERE 0"
                if bounds is not None
                else ""
            )
            rows = conn.execute(
                f"SELECT {','.join(names)} FROM {dataset.sqlite_table}{where} "  # nosec B608
                f"ORDER BY {','.join(dataset.primary_key)}",
                bounds or (),
            )
            scope.update(_digest(rows))
    return scopes


def _scope_rows(
    session: LakeSession,
    *,
    dataset: LakeDataset,
    paths: Sequence[Path],
    scope: Mapping[str, Any],
) -> Iterator[tuple[object, ...]]:
    if not paths:
        return
    column = "bundle_id" if "bundle_ids" in scope else dataset.date_column
    values = scope.get("bundle_ids", scope.get("days"))
    where = "" if values is None else f" WHERE {column} IN (SELECT unnest($values))"
    arguments = {"files": [str(path) for path in paths]}
    if values is not None:
        arguments["values"] = values
    cursor = session.connection.cursor()
    try:
        cursor.execute(
            f"SELECT {','.join(column.name for column in dataset.columns)} "  # nosec B608
            f"FROM read_parquet($files){where} ORDER BY {','.join(dataset.primary_key)}",
            arguments,
        )
        while batch := cursor.fetchmany(1 if dataset.has_binary_payload else 4096):
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
    """Verify actual fixed-release bytes before acknowledging any applied stage."""
    if (
        not {JPX_SOURCE_BUNDLES.name, JPX_SOURCE_RESPONSES.name} <= expected.keys()
        or set(expected) - _DATASETS.keys()
    ):
        raise ValueError("JPX publication readback plan is absent or unsupported")
    pointer = _serving_pointer(store).pointer
    if (
        pointer is None
        or pointer.release_id != generations["lake_release_id"]
        or pointer.manifest_sha256 != generations["lake_manifest_sha256"]
    ):
        raise LakePublishError("JPX readback differs from the fixed publication")
    directory.mkdir(parents=True, exist_ok=True)
    release = _resolve_serving_release(store, directory, pointer)
    cache = LakeObjectCache(root=directory, source=LocalMirrorSource(directory))
    counts = {}
    with lake_session() as session:
        for name, scope in expected.items():
            dataset = _DATASETS[name]
            manifest = release.dataset_manifests.get(name)
            if manifest is not None:
                accepted_dataset(release, dataset)
            days = scope.get("days", ())
            months = scope.get("months", ())
            periods = {
                (date.fromisoformat(day + "-01").year, date.fromisoformat(day + "-01").month)
                for day in months
            } | {(date.fromisoformat(day).year, date.fromisoformat(day).month) for day in days}
            parts = (
                tuple(
                    part
                    for part in manifest.partitions
                    if not ("days" in scope or "months" in scope)
                    or any(
                        part.values["year"] == year
                        and ("month" not in part.values or part.values["month"] == month)
                        for year, month in periods
                    )
                )
                if manifest is not None
                else ()
            )
            for part in parts:
                for obj in part.objects:
                    _fetch(store, directory, obj.key, expected_sha256=obj.sha256)
            paths = materialize_partitions(cache, dataset=dataset, partitions=parts)
            actual = _digest(_scope_rows(session, dataset=dataset, paths=paths, scope=scope))
            if actual != {"rows": scope["rows"], "sha256": scope["sha256"]}:
                raise LakePublishError("JPX original/projection publication readback differs")
            counts[name] = int(actual["rows"])
    return counts
