"""Export the market store into the lake, seal a release, and publish it to R2.

This is the write half of the daily cutover. Its read half is ``lake hydrate``, and the
two are joined by one rule: the serving release must still be the one the store was
filled from. That identity lives in the SQLite file itself: a sidecar can be restored or
copied independently and therefore cannot prove which rows the export is reading.

Every publication derives every partition from the store. The serving release is read
for two facts only — the origin the store must be bound to, and the history floor the
new release must still reach — and none of its partitions are carried.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import tempfile
import time
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from baibai_engine.batch_api import (
    L1ReleasePointer,
    LakeBuildError,
    LakeBuildReport,
    LakeFixedRelease,
    LakeReadError,
    LakeReleaseManifest,
    LakeStoreOrigin,
    LakeStoreOriginError,
    LocalMirrorSource,
    advance_lake_store_origin,
    canonical_lake_model_bytes,
    create_lake_l1_release,
    export_lake_legacy,
    lake_dataset_manifest_key,
    lake_mirror_path,
    lake_verified_git_commit,
    load_lake_model_json,
    resolve_release,
)

from .lake_publish import (
    Boto3R2Store,
    LakePublishError,
    ObjectStore,
    PointerSnapshot,
    publish_l1_release,
    read_pointer_snapshot,
)


@dataclass(frozen=True, slots=True)
class MarketLakePublishReport:
    release_id: str
    release_manifest_sha256: str
    data_as_of: str
    previous_release_id: str | None
    changed_partitions: Mapping[str, int]
    """Per dataset, how many partitions landed on a content-addressed key the mirror
    did not hold — the partitions whose bytes moved since the previous publication."""
    uploaded_objects: int
    uploaded_bytes: int

    def as_dict(self) -> dict[str, object]:
        return {
            "changed_partitions": dict(sorted(self.changed_partitions.items())),
            "data_as_of": self.data_as_of,
            "previous_release_id": self.previous_release_id,
            "release_id": self.release_id,
            "release_manifest_sha256": self.release_manifest_sha256,
            "uploaded_bytes": self.uploaded_bytes,
            "uploaded_objects": self.uploaded_objects,
        }


@dataclass(frozen=True, slots=True)
class _ServingCoverageOverride:
    origin: LakeStoreOrigin
    coverage_starts: Mapping[str, date]


def publish_market_lake(
    *,
    sqlite_path: Path,
    mirror_root: Path,
    store: ObjectStore,
    release_id: str | None = None,
    serving_coverage_override: _ServingCoverageOverride | None = None,
) -> MarketLakePublishReport:
    """Derive every partition from the store, seal a release, and publish it.

    The serving release is resolved for the origin the store must be bound to and for
    the history floor the policy check applies — a publication must not quietly serve
    less history than the release it replaces. None of its partitions are carried.
    """

    base_resolve_started = time.perf_counter()
    serving = _serving_pointer(store)
    previous = serving.pointer
    expected_store_origin = _pointer_origin(previous)
    if previous is None:
        if serving_coverage_override is not None:
            raise LakePublishError("serving coverage override requires a current release")
        published_coverage_start: Mapping[str, date] = {}
    elif serving_coverage_override is not None:
        if _pointer_origin(previous) != serving_coverage_override.origin:
            raise LakePublishError("serving coverage override identifies a different release")
        published_coverage_start = serving_coverage_override.coverage_starts
    else:
        serving_release = _resolve_serving_release(store, mirror_root, previous)
        published_coverage_start = {
            name: manifest.coverage_start
            for name, manifest in serving_release.dataset_manifests.items()
        }

    export_started = time.perf_counter()
    export = export_lake_legacy(
        sqlite_path=sqlite_path,
        mirror_root=mirror_root,
        producer_git_commit=lake_verified_git_commit(),
        expected_store_origin=expected_store_origin,
    )
    export_finished = time.perf_counter()
    with _export_manifest_snapshot(export.datasets, mirror_root) as manifest_paths:
        release_path, release = create_lake_l1_release(
            published_coverage_start=published_coverage_start,
            dataset_manifest_paths=manifest_paths,
            mirror_root=mirror_root,
            release_id=release_id,
        )
        release_payload = canonical_lake_model_bytes(release)
        release_sha256 = hashlib.sha256(release_payload).hexdigest()
        release_finished = time.perf_counter()
        published = publish_l1_release(
            mirror_root=mirror_root,
            release_manifest_path=release_path,
            expected_release_sha256=release_sha256,
            store=store,
            pointer_precondition=serving.precondition,
        )
    target_origin = LakeStoreOrigin(
        release_id=published.release_id,
        release_manifest_sha256=release_sha256,
    )
    try:
        advance_lake_store_origin(sqlite_path, expected=export.store_origin, target=target_origin)
    except LakeStoreOriginError as exc:
        raise LakePublishError(
            f"the L1 pointer advanced but the local market store origin did not: {exc}; "
            "hydrate the store from current before publishing again"
        ) from exc
    print(
        "lake publish phases: "
        f"base_resolve={export_started - base_resolve_started:.3f}s "
        f"seal_plan_export={export_finished - export_started:.3f}s "
        f"release_create={release_finished - export_finished:.3f}s "
        f"local_graph={published.local_graph_seconds:.3f}s "
        f"remote_closure={published.remote_closure_seconds:.3f}s "
        f"pointer={published.pointer_seconds:.3f}s",
        file=sys.stderr,
    )
    transfers = published.transfers.as_dict()
    return MarketLakePublishReport(
        release_id=published.release_id,
        release_manifest_sha256=release_sha256,
        data_as_of=release.data_as_of.isoformat(),
        previous_release_id=None if previous is None else previous.release_id,
        changed_partitions={
            name: item.new_objects for name, item in sorted(export.datasets.items())
        },
        uploaded_objects=int(transfers.get("uploaded_objects", 0)),
        uploaded_bytes=int(transfers.get("uploaded_bytes", 0)),
    )


def _serving_pointer(store: ObjectStore) -> PointerSnapshot:
    return read_pointer_snapshot(store)


def _pointer_origin(pointer: L1ReleasePointer | None) -> LakeStoreOrigin | None:
    if pointer is None:
        return None
    return LakeStoreOrigin(
        release_id=pointer.release_id,
        release_manifest_sha256=pointer.manifest_sha256,
    )


@contextmanager
def _export_manifest_snapshot(
    datasets: Mapping[str, LakeBuildReport], mirror_root: Path
) -> Iterator[list[Path]]:
    """Freeze the exact manifest models checked by the export into private bytes."""

    mirror_root.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(
        dir=mirror_root, prefix=".lake-publication-manifests-"
    ) as directory:
        root = Path(directory)
        paths: list[Path] = []
        for index, (name, item) in enumerate(sorted(datasets.items())):
            payload = canonical_lake_model_bytes(item.manifest)
            path = root / f"{index:02d}-{name.replace('.', '-')}.json"
            path.write_bytes(payload)
            paths.append(path)
        yield paths


def _resolve_serving_release(
    store: ObjectStore, mirror_root: Path, serving: L1ReleasePointer
) -> LakeFixedRelease:
    release_path = _fetch(
        store,
        mirror_root,
        serving.manifest_key,
        expected_sha256=serving.manifest_sha256,
    )
    try:
        release = load_lake_model_json(release_path.read_bytes(), LakeReleaseManifest)
    except ValueError:
        raise LakePublishError(
            f"serving release manifest is invalid: {serving.manifest_key}"
        ) from None
    for dataset_name, entry in sorted(release.datasets.items()):
        _fetch(
            store,
            mirror_root,
            lake_dataset_manifest_key(dataset=dataset_name, build_id=entry.build_id),
            expected_sha256=entry.manifest_sha256,
        )
    try:
        return resolve_release(
            LocalMirrorSource(mirror_root),
            serving.release_id,
            manifest_sha256=serving.manifest_sha256,
        )
    except LakeReadError as exc:
        raise LakePublishError(f"serving release identity validation failed: {exc}") from None


def _fetch(
    store: ObjectStore,
    mirror_root: Path,
    key: str,
    *,
    expected_sha256: str,
) -> Path:
    # Through the validating resolver rather than a join: these keys come off a remote
    # pointer, and a join would follow one that escaped the mirror.
    path = lake_mirror_path(mirror_root, key)
    if path.is_file() and _file_sha256(path) == expected_sha256:
        return path
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=path.parent, prefix=f".{path.name}.") as directory:
        candidate = Path(directory) / path.name
        store.download_file(key, candidate)
        actual_sha256 = _file_sha256(candidate)
        if actual_sha256 != expected_sha256:
            raise LakePublishError(
                f"downloaded lake object digest mismatch: {key}; "
                f"expected {expected_sha256}, got {actual_sha256}"
            )
        candidate.replace(path)
        _fsync_directory(path.parent)
    return path


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sqlite", type=Path, required=True)
    parser.add_argument("--mirror", type=Path, required=True)
    parser.add_argument("--bucket", default="baibai-stores")
    parser.add_argument("--release-id")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        report = publish_market_lake(
            sqlite_path=args.sqlite,
            mirror_root=args.mirror,
            store=Boto3R2Store(bucket=args.bucket),
            release_id=args.release_id,
        )
    except LakeBuildError as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    except LakePublishError as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    print(json.dumps(report.as_dict(), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
