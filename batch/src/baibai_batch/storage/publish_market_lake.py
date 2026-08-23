"""Export the market store into the lake, seal a release, and publish it to R2.

This is the write half of the daily cutover. Its read half is ``lake hydrate``, and the
two are joined by one rule: the serving release must still be the one the store was
filled from. An incremental export uses that release as its base; a full rebuild uses it
as the floor its sealed snapshot must contain. A run derived from another generation
could seal a graph missing current rows, and the pointer CAS would not notice — the CAS
protects the switch, not the store the graph was derived from.

An incremental publication writes only changed partitions and carries every other one by
reference from private copies of the validated base manifests. A full rebuild carries no
base partition and re-derives the whole store.
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
from pathlib import Path

from baibai_engine.batch_api import (
    L1ReleasePointer,
    LakeBuildError,
    LakeDatasetManifest,
    LakeFixedRelease,
    LakeReadError,
    LakeReleaseManifest,
    LocalMirrorSource,
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
    base_release_id: str | None
    changed_partitions: Mapping[str, int]
    created_objects: Mapping[str, int]
    uploaded_objects: int
    uploaded_bytes: int

    def as_dict(self) -> dict[str, object]:
        return {
            "base_release_id": self.base_release_id,
            "changed_partitions": dict(sorted(self.changed_partitions.items())),
            "created_objects": dict(sorted(self.created_objects.items())),
            "data_as_of": self.data_as_of,
            "release_id": self.release_id,
            "release_manifest_sha256": self.release_manifest_sha256,
            "uploaded_bytes": self.uploaded_bytes,
            "uploaded_objects": self.uploaded_objects,
        }


def publish_market_lake(
    *,
    sqlite_path: Path,
    mirror_root: Path,
    store: ObjectStore,
    expected_base_release_id: str | None,
    expected_base_manifest_sha256: str | None,
    origin_release_id: str | None = None,
    origin_manifest_sha256: str | None = None,
    release_id: str | None = None,
    full_rebuild: bool = False,
) -> MarketLakePublishReport:
    """Export the changed partitions on top of the serving release and publish them.

    ``full_rebuild`` drops the base and re-derives every partition from the store. The
    export refuses a base manifest whose transform fingerprint differs from the current
    one — a dependency bump that moves the Parquet writer version is enough — and the
    only way forward it names is a build without a base. Without this the recovery it
    demands has no publication path, so the daily batch stays broken until the fingerprint
    happens to match again.
    """

    _require_complete_identity("base", expected_base_release_id, expected_base_manifest_sha256)
    _require_complete_identity("origin", origin_release_id, origin_manifest_sha256)
    if full_rebuild and expected_base_release_id is not None:
        raise LakePublishError("a full rebuild has no base release to expect")
    if not full_rebuild and origin_release_id is not None:
        raise LakePublishError("a store origin is only named for a full rebuild")
    base_resolve_started = time.perf_counter()
    serving = _serving_pointer(store)
    base = serving.pointer
    if full_rebuild:
        _require_expected_release(
            base,
            origin_release_id,
            origin_manifest_sha256,
            role="origin",
        )
        replaced = None if base is None else _resolve_base_release(store, mirror_root, base)
        fixed_base = None
    else:
        _require_expected_release(
            base,
            expected_base_release_id,
            expected_base_manifest_sha256,
            role="base",
        )
        fixed_base = None if base is None else _resolve_base_release(store, mirror_root, base)
        replaced = None

    with _base_manifest_snapshot(fixed_base) as base_manifests:
        export_started = time.perf_counter()
        export = export_lake_legacy(
            sqlite_path=sqlite_path,
            mirror_root=mirror_root,
            producer_git_commit=lake_verified_git_commit(),
            base_manifest_paths=base_manifests,
            audit_full_history=False,
        )
    if full_rebuild:
        _require_no_rows_lost(
            {name: item.manifest for name, item in export.datasets.items()},
            replaced,
        )
    export_finished = time.perf_counter()
    release_path, release = create_lake_l1_release(
        dataset_manifest_paths=[item.manifest_path for item in export.datasets.values()],
        mirror_root=mirror_root,
        release_id=release_id,
    )
    release_finished = time.perf_counter()
    published = publish_l1_release(
        mirror_root=mirror_root,
        release_manifest_path=release_path,
        store=store,
        pointer_precondition=serving.precondition,
    )
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
        release_manifest_sha256=hashlib.sha256(release_path.read_bytes()).hexdigest(),
        data_as_of=release.data_as_of.isoformat(),
        base_release_id=None if base is None else base.release_id,
        changed_partitions={
            name: len(item.changed_partitions) for name, item in sorted(export.datasets.items())
        },
        created_objects={
            name: item.created_objects for name, item in sorted(export.datasets.items())
        },
        uploaded_objects=int(transfers.get("uploaded_objects", 0)),
        uploaded_bytes=int(transfers.get("uploaded_bytes", 0)),
    )


def _serving_pointer(store: ObjectStore) -> PointerSnapshot:
    return read_pointer_snapshot(store)


def _require_complete_identity(
    role: str, release_id: str | None, manifest_sha256: str | None
) -> None:
    if (release_id is None) != (manifest_sha256 is None):
        raise LakePublishError(
            f"a {role} release is named by its id and its manifest digest, or not at all"
        )


def _require_expected_release(
    base: L1ReleasePointer | None,
    expected_release_id: str | None,
    expected_manifest_sha256: str | None,
    *,
    role: str,
) -> None:
    """Refuse unless the lake still serves the release this store was filled from.

    The check is on the full identity rather than the name, for the same reason the
    fill checks it that way: a release ID republished over different bytes would pass
    a name comparison while naming a different graph.
    """

    if expected_release_id is None:
        if base is not None:
            raise LakePublishError(
                f"the lake already serves release {base.release_id}; "
                f"name it as the store {role}, or hydrate from it first"
            )
        return
    if base is None:
        raise LakePublishError(
            f"the lake serves no release, but {expected_release_id} was named as the base"
        )
    if (base.release_id, base.manifest_sha256) != (expected_release_id, expected_manifest_sha256):
        raise LakePublishError(
            f"the lake moved to release {base.release_id} since this store was hydrated "
            f"from {expected_release_id}; hydrate again before publishing"
        )


def _require_no_rows_lost(
    exported_manifests: Mapping[str, LakeDatasetManifest], replaced: LakeFixedRelease | None
) -> None:
    """Refuse a full rebuild that would publish fewer rows than the release it replaces.

    A differential build cannot lose history: unchanged partitions are carried by
    reference from the base. A full rebuild has no such floor — it publishes exactly what
    the store holds, so a store that is behind the lake would seal a release missing the
    difference, and the pointer CAS would not notice because the switch itself is valid.
    The base release's own per-dataset totals are the floor, and they are already in the
    manifest this publication would replace.
    """

    if replaced is None:
        return
    for name, entry in sorted(replaced.manifest.datasets.items()):
        exported = exported_manifests.get(name)
        held = 0 if exported is None else exported.totals.rows
        if held < entry.totals.rows:
            raise LakePublishError(
                f"{name} holds {held} row(s) in the sealed export but release "
                f"{replaced.release_id} publishes {entry.totals.rows}; a full rebuild "
                "would drop the difference. Hydrate the store from the serving release first"
            )


@contextmanager
def _base_manifest_snapshot(
    fixed: LakeFixedRelease | None,
) -> Iterator[dict[str, Path]]:
    """Give the writer private bytes from the release that was already validated."""

    if fixed is None:
        yield {}
        return
    with tempfile.TemporaryDirectory(prefix="baibai-lake-base-manifests-") as directory:
        root = Path(directory)
        paths: dict[str, Path] = {}
        for index, (dataset_name, manifest) in enumerate(sorted(fixed.dataset_manifests.items())):
            payload = canonical_lake_model_bytes(manifest)
            if hashlib.sha256(payload).hexdigest() != fixed.dataset_manifest_sha256[dataset_name]:
                raise LakePublishError(
                    f"validated base manifest cannot reproduce its identity: {dataset_name}"
                )
            path = root / f"{index:02d}.json"
            path.write_bytes(payload)
            paths[dataset_name] = path
        yield paths


def _resolve_base_release(
    store: ObjectStore, mirror_root: Path, base: L1ReleasePointer
) -> LakeFixedRelease:
    release_path = _fetch(
        store,
        mirror_root,
        base.manifest_key,
        expected_sha256=base.manifest_sha256,
    )
    try:
        release = load_lake_model_json(release_path.read_bytes(), LakeReleaseManifest)
    except ValueError:
        raise LakePublishError(f"base release manifest is invalid: {base.manifest_key}") from None
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
            base.release_id,
            manifest_sha256=base.manifest_sha256,
        )
    except LakeReadError as exc:
        raise LakePublishError(f"base release identity validation failed: {exc}") from None


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
    parser.add_argument(
        "--base-release",
        help="the release this store was hydrated from; omit only for the first publication",
    )
    parser.add_argument(
        "--base-manifest-sha256", help="required digest when --base-release is used"
    )
    parser.add_argument(
        "--origin-release",
        help="the release a full-rebuild store was hydrated from",
    )
    parser.add_argument(
        "--origin-manifest-sha256",
        help="required digest when --origin-release is used",
    )
    parser.add_argument("--release-id")
    parser.add_argument(
        "--full-rebuild",
        action="store_true",
        help=(
            "re-derive every partition from the store instead of carrying the base "
            "release's unchanged ones. Required after the export transform fingerprint "
            "moves — a Parquet writer version bump is enough — because the differential "
            "export refuses a base built under the previous one"
        ),
    )
    return parser


RECOVERY_RUNBOOK_SECTION = "fingerprint 変更後の full rebuild"
"""The `batch/OPERATIONS.md` section that carries the command this failure needs."""

_FULL_REBUILD_RECOVERY = (
    "no retry clears this: the base release was built under a different export "
    "fingerprint, and every scheduled batch stops here until a full rebuild is "
    f'published. Follow "{RECOVERY_RUNBOOK_SECTION}" in batch/OPERATIONS.md.'
)


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        report = publish_market_lake(
            sqlite_path=args.sqlite,
            mirror_root=args.mirror,
            store=Boto3R2Store(bucket=args.bucket),
            expected_base_release_id=args.base_release,
            expected_base_manifest_sha256=args.base_manifest_sha256,
            origin_release_id=args.origin_release,
            origin_manifest_sha256=args.origin_manifest_sha256,
            release_id=args.release_id,
            full_rebuild=args.full_rebuild,
        )
    except LakeBuildError as error:
        # The export refuses a base built under a different transform fingerprint. That
        # is the guard working, but the run log used to end in a traceback that named no
        # way out — and the state does not clear on its own, so every scheduled batch
        # stops in the same place until a full rebuild is published. The pointer lives
        # here rather than in the writer's message because the writer's source is one of
        # the three files the fingerprint is taken over: editing this wording there would
        # move the fingerprint and demand the very rebuild it describes.
        print(f"error: {error}", file=sys.stderr)
        print(f"error: {_FULL_REBUILD_RECOVERY}", file=sys.stderr)
        return 1
    except LakePublishError as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    print(json.dumps(report.as_dict(), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
