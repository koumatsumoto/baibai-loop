"""Fail before economic publication on an incompatible pulled Market generation."""

from __future__ import annotations

import argparse
import sqlite3
import sys
from collections.abc import Callable
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path

from baibai_engine.storage.objects.r2 import Boto3R2Store, LakePublishError, ObjectStore
from baibai_engine.storage.snapshots.models import ReleaseManifest, SQLiteSnapshotSourceRef
from baibai_engine.storage.snapshots.reader import (
    LakeReadError,
    read_release_manifest,
    resolve_current_release,
    resolve_release,
)
from baibai_engine.storage.sqlite.source_cache.lake_origin import (
    LakeStoreOrigin,
    LakeStoreOriginError,
    read_lake_store_origin_from_connection,
)
from baibai_engine.storage.sqlite.source_cache.schema import (
    SQLITE_SCHEMA_VERSION,
    SQLiteSchemaError,
    connect_current,
)
from baibai_jobs.publication.datasets import LAKE_DATASETS
from baibai_jobs.publication.lease import LeaseError, _load_handle, verify_held
from baibai_jobs.publication.machine_bundle import decode_machine_receipt
from baibai_jobs.publication.machine_origin import origin_fields
from baibai_jobs.publication.policy import (
    edinet_metrics_contract_is_current,
    release_policy_for_profile,
)
from baibai_jobs.publication.snapshot_release import read_pointer_snapshot


class MachinePreflightError(ValueError):
    """A safe-to-report prerequisite for the daily machine run is absent."""


@dataclass(frozen=True, slots=True)
class _ReleaseMetadata:
    get_bytes: Callable[[str], bytes]

    def read_bytes(self, key: str) -> bytes:
        return self.get_bytes(key)


def _require_active_edinet_metrics(manifest: ReleaseManifest) -> None:
    if edinet_metrics_contract_is_current(manifest) is not True:
        raise MachinePreflightError("daily Market requires the current EDINET metrics contract")


def check_daily_input(store: ObjectStore, *, market: Path, receipt: bytes) -> bool:
    """Check the already paired transfer without fetching machine bodies again.

    True means a strictly newer Lake was published before the two machine stores.
    This authorizes only the normal forward hydrate, never source seals or a rollback.
    """
    connection = connect_current(market)
    if connection is None:
        raise MachinePreflightError("pulled Market is missing")
    with closing(connection):
        supplied = read_lake_store_origin_from_connection(connection)
    if supplied is None:
        raise MachinePreflightError("pulled Market has no Lake origin")

    manifest = decode_machine_receipt(receipt)
    if "daily_publication" in manifest:
        daily = manifest["daily_publication"]
        run = daily.get("run") if isinstance(daily, dict) else None
        if not isinstance(run, str):
            raise MachinePreflightError("pulled machine receipt has invalid daily provenance")
        if daily != origin_fields(run, supplied):
            raise MachinePreflightError("pulled machine receipt does not bind this Market origin")

    pointer = read_pointer_snapshot(store).pointer
    if pointer is None:
        raise MachinePreflightError("current Lake is missing")
    current = LakeStoreOrigin(pointer.release_id, pointer.manifest_sha256)
    source = _ReleaseMetadata(store.get_bytes)
    target = read_release_manifest(
        source, current.release_id, manifest_sha256=current.release_manifest_sha256
    )
    policy = release_policy_for_profile("production")
    permitted = {item.dataset for item in policy.datasets}
    required = {item.dataset for item in policy.datasets if item.required}
    if not required <= target.datasets.keys() <= permitted:
        raise MachinePreflightError("current Lake does not declare the required Market datasets")
    # Historical decoders do not authorize using a stale release in the daily writer.
    _require_active_edinet_metrics(target)
    for name, entry in target.datasets.items():
        contract = LAKE_DATASETS[name].read_contract(entry.contract_version)
        if entry.layer != contract.layer:
            raise MachinePreflightError("current Lake declares an incompatible dataset layer")
    if supplied == current:
        return False

    # A failed prior daily can move only the Lake. Do not confuse that with a replaced
    # release ID, a downgrade, or a new writer's independent SQLite contract.
    if supplied.release_id == current.release_id:
        raise MachinePreflightError("current Lake changed the pulled release's manifest digest")
    previous = resolve_release(
        source, supplied.release_id, manifest_sha256=supplied.release_manifest_sha256
    )
    _require_active_edinet_metrics(previous.manifest)
    if target.created_at <= previous.manifest.created_at:
        raise MachinePreflightError("forward hydrate needs a newer Lake than the pulled origin")
    fixed = resolve_current_release(
        source,
        policy=policy,
        catalog=LAKE_DATASETS,
        published_coverage_start={
            name: manifest.coverage_start for name, manifest in previous.dataset_manifests.items()
        },
    )
    if (fixed.release_id, fixed.manifest_sha256) != (
        current.release_id,
        current.release_manifest_sha256,
    ):
        raise MachinePreflightError("current Lake changed during forward admission")
    # A fresh export clock alone does not distinguish retry from restoring older data.
    # The release policy has already checked history floors and missing preserved datasets.
    for rule in policy.datasets:
        if not rule.carries_history:
            continue
        before = previous.dataset_manifests.get(rule.dataset)
        after = fixed.dataset_manifests.get(rule.dataset)
        if before is not None and after is not None and after.data_as_of < before.data_as_of:
            raise MachinePreflightError("forward hydrate would narrow the retained Lake history")
    schemas = {
        origin.schema_version
        for dataset in fixed.dataset_manifests.values()
        for partition in dataset.partitions
        for origin in partition.sources
        if isinstance(origin, SQLiteSnapshotSourceRef)
    }
    if schemas != {SQLITE_SCHEMA_VERSION}:
        raise MachinePreflightError(
            "forward hydrate needs a Lake sealed with the current Market schema"
        )
    return True


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--market", type=Path, required=True)
    parser.add_argument("--receipt", type=Path, required=True)
    parser.add_argument("--bucket", required=True)
    parser.add_argument("--lease-handle", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        handle = _load_handle(args.lease_handle)
        with closing(Boto3R2Store(bucket=args.bucket)) as store:
            verify_held(store, handle)
            forward = check_daily_input(
                store, market=args.market, receipt=args.receipt.read_bytes()
            )
    except MachinePreflightError as error:
        print(f"daily machine preflight: {error}", file=sys.stderr)
        return 1
    except (
        LakePublishError,
        LakeReadError,
        LakeStoreOriginError,
        LeaseError,
        SQLiteSchemaError,
        sqlite3.Error,
        KeyError,
        OSError,
        ValueError,
    ) as error:
        # Do not expose endpoint, release identifier, path or raw store/receipt content.
        print(f"daily machine preflight: stopped ({type(error).__name__})", file=sys.stderr)
        return 1
    print(f"daily machine preflight: {'forward-hydrate' if forward else 'paired'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
