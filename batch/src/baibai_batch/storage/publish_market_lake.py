"""Export the market store into the lake, seal a release, and publish it to R2.

This is the write half of the daily cutover. Its read half is ``lake hydrate``, and the
two are joined by one rule: the release this publication builds on must still be the one
the store was filled from. A run that exported on top of a release someone else had
already superseded would seal a graph missing their rows, and the pointer CAS would not
notice — the CAS protects the switch, not the base the export was derived from.

Only the partitions whose rows changed are written; every other partition is carried by
reference from the base manifests, so the objects that move are proportional to the day
rather than to the history.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from baibai_engine.batch_api import (
    L1ReleasePointer,
    LakeReleaseManifest,
    create_lake_l1_release,
    export_lake_legacy,
    lake_current_l1_pointer_key,
    lake_dataset_manifest_key,
    lake_mirror_path,
    lake_verified_git_commit,
    load_lake_model_json,
)

from .lake_publish import AwsCliR2Store, LakePublishError, ObjectStore, publish_l1_release


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
    release_id: str | None = None,
) -> MarketLakePublishReport:
    """Export the changed partitions on top of the serving release and publish them."""

    if (expected_base_release_id is None) != (expected_base_manifest_sha256 is None):
        raise LakePublishError(
            "a base release is named by its id and its manifest digest, or not at all"
        )
    base = _serving_pointer(store)
    _require_expected_base(base, expected_base_release_id, expected_base_manifest_sha256)
    base_manifests = _base_manifest_paths(store, mirror_root, base)

    export = export_lake_legacy(
        sqlite_path=sqlite_path,
        mirror_root=mirror_root,
        producer_git_commit=lake_verified_git_commit(),
        base_manifest_paths=base_manifests,
        audit_full_history=False,
    )
    release_path, release = create_lake_l1_release(
        dataset_manifest_paths=[item.manifest_path for item in export.datasets.values()],
        mirror_root=mirror_root,
        release_id=release_id,
    )
    published = publish_l1_release(
        mirror_root=mirror_root,
        release_manifest_path=release_path,
        store=store,
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


def _serving_pointer(store: ObjectStore) -> L1ReleasePointer | None:
    remote = store.head(lake_current_l1_pointer_key())
    if remote is None:
        return None
    return load_lake_model_json(store.get_bytes(lake_current_l1_pointer_key()), L1ReleasePointer)


def _require_expected_base(
    base: L1ReleasePointer | None,
    expected_release_id: str | None,
    expected_manifest_sha256: str | None,
) -> None:
    """Refuse unless the lake still serves the release this store was filled from.

    The check is on the full identity rather than the name, for the same reason the
    projection checks it that way: a release ID republished over different bytes would
    pass a name comparison while naming a different graph.
    """

    if expected_release_id is None:
        if base is not None:
            raise LakePublishError(
                f"the lake already serves release {base.release_id}; "
                "name it as the base, or hydrate from it first"
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


def _base_manifest_paths(
    store: ObjectStore, mirror_root: Path, base: L1ReleasePointer | None
) -> dict[str, Path]:
    """Fetch the base release's dataset manifests into the mirror and name them.

    The manifests are what the export carries unchanged partitions by, so they are the
    only part of the previous release a differential build has to read. The objects they
    name stay where they are.
    """

    if base is None:
        return {}
    release_path = _fetch(store, mirror_root, base.manifest_key)
    release = load_lake_model_json(release_path.read_bytes(), LakeReleaseManifest)
    paths: dict[str, Path] = {}
    for dataset_name, entry in sorted(release.datasets.items()):
        key = lake_dataset_manifest_key(dataset=dataset_name, build_id=entry.build_id)
        paths[dataset_name] = _fetch(store, mirror_root, key)
    return paths


def _fetch(store: ObjectStore, mirror_root: Path, key: str) -> Path:
    # Through the validating resolver rather than a join: these keys come off a remote
    # pointer, and a join would follow one that escaped the mirror.
    path = lake_mirror_path(mirror_root, key)
    if path.is_file():
        return path
    path.parent.mkdir(parents=True, exist_ok=True)
    store.download_file(key, path)
    return path


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
    parser.add_argument("--release-id")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        report = publish_market_lake(
            sqlite_path=args.sqlite,
            mirror_root=args.mirror,
            store=AwsCliR2Store(bucket=args.bucket),
            expected_base_release_id=args.base_release,
            expected_base_manifest_sha256=args.base_manifest_sha256,
            release_id=args.release_id,
        )
    except LakePublishError as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    print(json.dumps(report.as_dict(), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
