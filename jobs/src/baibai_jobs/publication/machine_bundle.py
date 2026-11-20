"""Read the published market/runs receipt; never fetch the SQLite bodies or write to R2."""

from __future__ import annotations

import argparse
import json
import os
import sys
from contextlib import closing
from pathlib import Path

from baibai_engine.storage.objects.r2 import Boto3R2Store, LakePublishError, ObjectStore
from baibai_engine.storage.paths import MARKET_DB_PATH
from baibai_engine.storage.snapshots.release import LakeReleasePointer
from baibai_engine.storage.sqlite.source_cache.lake_origin import (
    LakeStoreOrigin,
    LakeStoreOriginError,
    read_lake_store_origin,
)
from baibai_jobs.publication.snapshot_release import read_pointer_snapshot


def read_published_machine(store: ObjectStore) -> tuple[LakeReleasePointer, dict[str, str]]:
    pointer = read_pointer_snapshot(store).pointer
    if pointer is None:
        raise ValueError("a published lake release is required")
    try:
        manifest = json.loads(store.get_bytes("machine-manifest.json"))
    except (KeyError, ValueError, UnicodeDecodeError) as error:
        raise LakePublishError("published machine bundle receipt is unavailable") from error
    if not isinstance(manifest, dict) or manifest.get("schema_version") != 1:
        raise LakePublishError("published machine bundle receipt is invalid")
    keys = manifest.get("keys")
    if not isinstance(keys, dict):
        raise LakePublishError("published machine bundle receipt is invalid")
    versions: dict[str, str] = {}
    for key in ("market.sqlite", "runs.sqlite"):
        recorded = keys.get(key)
        current = store.head(key)
        if (
            not isinstance(recorded, str)
            or not recorded
            or current is None
            or current.etag != recorded
        ):
            raise LakePublishError("published machine bundle differs from its receipt")
        versions[key] = recorded
    return pointer, versions


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Guard a display-only recovery on one market release"
    )
    parser.add_argument("release_id")
    parser.add_argument("manifest_sha256")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--capture", type=Path, help="save the verified ETag pair before pulling")
    mode.add_argument("--verify", type=Path, help="recheck the saved pair and local market origin")
    args = parser.parse_args(argv)
    try:
        expected = LakeStoreOrigin(args.release_id, args.manifest_sha256)
        if args.capture is None and args.verify is None:
            return 0  # Syntax only, before credentials or a lease.
        with closing(
            Boto3R2Store(bucket=os.environ.get("R2_STORES_BUCKET", "baibai-stores"))
        ) as store:
            pointer, versions = read_published_machine(store)
        if (pointer.release_id, pointer.manifest_sha256) != (
            expected.release_id,
            expected.release_manifest_sha256,
        ):
            raise ValueError("requested market release is not current")
        if args.capture is not None:
            with args.capture.open("x", encoding="utf-8") as output:
                json.dump(versions, output)
        else:
            if json.loads(args.verify.read_bytes()) != versions:
                raise ValueError("published machine pair changed since the recovery began")
            if read_lake_store_origin(MARKET_DB_PATH) != expected:
                raise ValueError("local market origin differs from the requested release")
    except (LakePublishError, LakeStoreOriginError, ValueError, OSError) as error:
        print(f"market recovery stopped: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
