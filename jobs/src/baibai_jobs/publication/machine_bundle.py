"""Read the published market/runs receipt; never fetch the SQLite bodies or write to R2."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
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
from baibai_jobs.publication.machine_origin import origin_fields
from baibai_jobs.publication.snapshot_release import read_pointer_snapshot


def read_published_machine(
    store: ObjectStore,
    *,
    expected_receipt_sha256: str | None = None,
    expected_origin: dict[str, str] | None = None,
) -> tuple[LakeReleasePointer, dict[str, str]]:
    """Read a coherent pair and honor any daily release binding it already carries."""
    pointer = read_pointer_snapshot(store).pointer
    if pointer is None:
        raise ValueError("a published lake release is required")
    try:
        receipt = store.get_bytes("machine-manifest.json")
        if (
            expected_receipt_sha256 is not None
            and hashlib.sha256(receipt).hexdigest() != expected_receipt_sha256
        ):
            raise LakePublishError("published machine receipt differs from the requested run")
        manifest = json.loads(receipt)
    except (KeyError, ValueError, UnicodeDecodeError) as error:
        raise LakePublishError("published machine bundle receipt is unavailable") from error
    if not isinstance(manifest, dict) or manifest.get("schema_version") != 1:
        raise LakePublishError("published machine bundle receipt is invalid")
    daily = manifest.get("daily_publication")
    if expected_origin is not None and daily != expected_origin:
        raise LakePublishError("machine receipt is not from the requested daily run and release")
    # The lake pointer may already have advanced while both machine stores still
    # match the previous receipt. Do not use that mixed state for seals or inputs.
    if "daily_publication" in manifest and (
        not isinstance(daily, dict)
        or daily.get("release_id") != pointer.release_id
        or daily.get("release_manifest_sha256") != pointer.manifest_sha256
    ):
        raise LakePublishError("published machine receipt differs from the current lake release")
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
    parser.add_argument("original_run", help="original daily run ID and attempt, e.g. 12345-1")
    parser.add_argument(
        "machine_receipt_sha256", help="receipt SHA-256 from the original core upload"
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--capture", type=Path, help="save the verified ETag pair before pulling")
    mode.add_argument("--verify", type=Path, help="recheck the saved pair and local market origin")
    args = parser.parse_args(argv)
    try:
        expected = LakeStoreOrigin(args.release_id, args.manifest_sha256)
        origin = origin_fields(args.original_run, expected)
        if re.fullmatch(r"[0-9a-f]{64}", args.machine_receipt_sha256) is None:
            raise ValueError("invalid machine receipt SHA-256 from the original run")
        if args.capture is None and args.verify is None:
            return 0  # Syntax only, before credentials or a lease.
        with closing(
            Boto3R2Store(bucket=os.environ.get("R2_STORES_BUCKET", "baibai-stores"))
        ) as store:
            _, versions = read_published_machine(
                store, expected_receipt_sha256=args.machine_receipt_sha256, expected_origin=origin
            )
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
