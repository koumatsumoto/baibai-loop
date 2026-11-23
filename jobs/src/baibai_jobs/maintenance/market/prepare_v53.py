"""Prepare one private Market v53 copy from a sealed, hydrated v52 input."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sqlite3
import sys
from collections.abc import Sequence
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory

from baibai_engine.storage.filesystem import fsync_directory
from baibai_engine.storage.snapshots.keys import validate_sha256
from baibai_engine.storage.snapshots.models import SQLiteSnapshotSourceRef, validate_release_policy
from baibai_engine.storage.snapshots.objects import LocalMirrorSource
from baibai_engine.storage.snapshots.reader import LakeReadError, accepted_dataset, resolve_release
from baibai_engine.storage.snapshots.sources import sha256_file
from baibai_engine.storage.sqlite.snapshot import validate_snapshot
from baibai_engine.storage.sqlite.source_cache.lake_origin import (
    LakeStoreOrigin,
    LakeStoreOriginError,
    read_lake_store_origin_from_connection,
)
from baibai_engine.storage.sqlite.source_cache.schema import (
    SQLITE_SCHEMA_VERSION,
    SQLiteSchemaError,
    validate_current_schema,
)
from baibai_jobs.publication.datasets import LAKE_DATASETS
from baibai_jobs.publication.policy import release_policy_for_profile

_METRICS = "edinet.metrics"
_FROM, _TO = 52, 53


def _require_no_sidecars(path: Path) -> None:
    if any(os.path.lexists(f"{path}{suffix}") for suffix in ("-wal", "-shm", "-journal")):
        raise ValueError("Market snapshots must not have SQLite sidecars")


def _hydrated_counts(mirror: Path, origin: LakeStoreOrigin) -> dict[str, int]:
    fixed = resolve_release(
        LocalMirrorSource(mirror),
        origin.release_id,
        manifest_sha256=origin.release_manifest_sha256,
    )
    if fixed.dataset_manifest(_METRICS).contract_version != 3:
        raise ValueError("Market v52 preparation requires an EDINET metrics v3 release")
    versions = {
        ref.schema_version
        for dataset in fixed.dataset_manifests.values()
        for partition in dataset.partitions
        for ref in partition.sources
        if isinstance(ref, SQLiteSnapshotSourceRef)
    }
    if versions != {_FROM}:
        raise ValueError("the old release must name a Market v52 seal")
    catalog = {
        name: accepted_dataset(fixed, descriptor) if name in fixed.dataset_manifests else descriptor
        for name, descriptor in LAKE_DATASETS.items()
    }
    validate_release_policy(
        fixed.manifest,
        fixed.dataset_manifests,
        policy=release_policy_for_profile("production"),
        catalog=catalog,
    )
    return {
        catalog[name].sqlite_table: m.totals.rows for name, m in fixed.dataset_manifests.items()
    }


def _convert(staged: Path, origin: LakeStoreOrigin, counts: dict[str, int]) -> None:
    with closing(
        sqlite3.connect(staged.as_uri() + "?mode=rw", uri=True, isolation_level=None)
    ) as connection:
        connection.execute("BEGIN IMMEDIATE")
        if connection.execute("PRAGMA user_version").fetchone() != (_FROM,):
            raise ValueError("source Market must have SQLite schema 52")
        if read_lake_store_origin_from_connection(connection) != origin:
            raise ValueError("source Lake origin differs from the fixed v52 release")
        for table, expected in counts.items():
            # Table names come only from the owned, validated catalog above.
            if connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone() != (expected,):  # nosec B608
                raise ValueError("source Market is not fully hydrated at the fixed release")
        connection.execute(
            "ALTER TABLE edinet_metrics ADD COLUMN parent_attributable_equity_json TEXT"
        )
        connection.execute("PRAGMA user_version=53")
        validate_current_schema(connection)
        connection.commit()


def prepare_market_v53_copy(
    *,
    source: Path,
    source_sha256: str,
    mirror: Path,
    release_id: str,
    manifest_sha256: str,
    output: Path,
) -> dict[str, str | int]:
    """Convert only exact copied bytes. The origin still names v52, not a publication."""
    if SQLITE_SCHEMA_VERSION != _TO or LAKE_DATASETS[_METRICS].contract_version != 4:
        raise ValueError("this one-time preparation requires the Market v53 producer")
    validate_sha256(source_sha256)
    origin = LakeStoreOrigin(release_id, manifest_sha256)
    source = source.resolve(strict=True)
    output = output.absolute()
    if os.path.lexists(output):
        raise FileExistsError("Market v53 output must not exist")
    if not source.is_file():
        raise ValueError("source must be a standalone Market file")
    _require_no_sidecars(source)
    _require_no_sidecars(output)
    counts = _hydrated_counts(mirror, origin)
    with TemporaryDirectory(dir=output.parent, prefix=".prepare-market-v53-") as directory:
        staged = Path(directory) / "market.sqlite"
        with source.open("rb") as old, staged.open("xb") as copy:
            shutil.copyfileobj(old, copy, length=1024 * 1024)
        if sha256_file(staged) != source_sha256:
            raise ValueError("source SHA-256 differs from the verified v52 copy")
        _require_no_sidecars(source)
        _convert(staged, origin, counts)
        _require_no_sidecars(staged)
        validate_snapshot(staged)
        digest = sha256_file(staged)
        staged.chmod(0o600)
        with staged.open("rb") as ready:
            os.fsync(ready.fileno())
        _require_no_sidecars(source)
        _require_no_sidecars(output)
        # Same-filesystem exclusive publication: another writer's file is never replaced.
        os.link(staged, output)
        fsync_directory(output.parent)
    return {
        "status": "prepared_local_market_copy",
        "schema_version": _TO,
        "source_sha256": source_sha256,
        "output_sha256": digest,
        "release_id": origin.release_id,
        "manifest_sha256": origin.release_manifest_sha256,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--source-sha256", required=True)
    parser.add_argument("--mirror", type=Path, required=True)
    parser.add_argument("--release-id", required=True)
    parser.add_argument("--manifest-sha256", required=True)
    parser.add_argument("--output", type=Path, required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    try:
        report = prepare_market_v53_copy(**vars(build_parser().parse_args(argv)))
    except (
        LakeReadError,
        LakeStoreOriginError,
        SQLiteSchemaError,
        OSError,
        ValueError,
        sqlite3.Error,
    ) as error:
        print(f"Market v53 copy not verified: {type(error).__name__}: {error}", file=sys.stderr)
        return 1
    print(json.dumps(report, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
