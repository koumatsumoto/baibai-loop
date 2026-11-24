"""Read one current Machine pair into disposable working files."""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import tempfile
from collections.abc import Iterator
from contextlib import closing, contextmanager, suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from baibai_engine.storage.filesystem import fsync_directory
from baibai_engine.storage.objects.r2 import (
    Boto3R2Store,
    LakeCASConflict,
    LakePublishError,
    ObjectStore,
    R2Error,
    R2OperationStopped,
    R2PinnedObjectMismatch,
)
from baibai_engine.storage.snapshots.keys import release_manifest_key
from baibai_engine.storage.snapshots.objects import LakeObjectSource, open_lake
from baibai_engine.storage.snapshots.reader import FixedRelease, resolve_policy_release
from baibai_engine.storage.snapshots.release import LakeReleasePointer
from baibai_engine.storage.snapshots.retention import exclusive_lock
from baibai_engine.storage.sqlite.snapshot import validate_snapshot
from baibai_engine.storage.sqlite.source_cache.lake_origin import read_lake_store_origin
from baibai_engine.storage.sqlite.source_cache.schema import SQLITE_SCHEMA_VERSION
from baibai_jobs.publication.datasets import LAKE_DATASETS
from baibai_jobs.publication.lease import _load_handle, verify_held
from baibai_jobs.publication.machine_current import (
    MachineMemberUnavailable,
    machine_publication_ids,
    require_market_origin,
    require_runs_publication,
)
from baibai_jobs.publication.policy import (
    edinet_metrics_contract_is_current,
    release_policy_for_profile,
)
from baibai_jobs.publication.snapshot_release import PointerSnapshot, read_pointer_snapshot
from baibai_jobs.publication.snapshot_write_cli import hydrate_release


class MachineDownloadStore(ObjectStore, Protocol):
    def download_file(
        self,
        key: str,
        path: Path,
        *,
        expect_bytes: int | None = None,
        expect_etag: str | None = None,
    ) -> None: ...


@dataclass(frozen=True, slots=True)
class PulledMachine:
    current: PointerSnapshot
    release: FixedRelease
    market: Path
    runs: Path | None


def resolve_machine_lake(
    source: LakeObjectSource, pointer: LakeReleasePointer, *, portfolio_only: bool = False
) -> FixedRelease:
    """Validate the pinned release under production policy, including for portfolio reads."""
    release = resolve_policy_release(
        source, pointer, catalog=LAKE_DATASETS, policy=release_policy_for_profile("production")
    )
    if not portfolio_only and edinet_metrics_contract_is_current(release.manifest) is False:
        raise LakePublishError("current Market requires the current EDINET metrics contract")
    return release


@contextmanager
def pull_current_machine(
    store: MachineDownloadStore,
    source: LakeObjectSource,
    *,
    directory: Path,
    portfolio_only: bool = False,
) -> Iterator[PulledMachine]:
    """Expose no working files until every requested member has been accepted together.

    The source reads the same bucket's Lake manifests. Current is not re-resolved
    while hydrating the verified temporary Market.
    """
    directory.mkdir(parents=True, exist_ok=True)
    before = read_pointer_snapshot(store)
    for attempt in range(2):
        pointer = before.pointer
        if pointer is None or pointer.machine is None:
            raise MachineMemberUnavailable("current does not name a machine pair")
        release = resolve_machine_lake(source, pointer, portfolio_only=portfolio_only)
        with tempfile.TemporaryDirectory(dir=directory, prefix=".machine-pull-") as tmp:
            market = Path(tmp) / "market.sqlite"
            runs = None if portfolio_only else Path(tmp) / "runs.sqlite"
            try:
                item = pointer.machine.market
                store.download_file(item.key, market, expect_etag=item.etag, expect_bytes=item.size)
                if runs is not None:
                    item = pointer.machine.runs
                    store.download_file(
                        item.key, runs, expect_etag=item.etag, expect_bytes=item.size
                    )
            except (R2Error, LakeCASConflict) as error:
                moved = isinstance(error, (R2PinnedObjectMismatch, LakeCASConflict)) or (
                    error.http_status == 404
                )
                if attempt or not moved:
                    raise
                replacement = read_pointer_snapshot(store)
                if replacement.precondition == before.precondition:
                    raise
                before = replacement
                continue
            if validate_snapshot(market) != SQLITE_SCHEMA_VERSION:
                raise sqlite3.DatabaseError("current Market requires the current SQLite schema")
            require_market_origin(pointer, market)
            if runs is not None:
                validate_snapshot(runs)
                require_runs_publication(pointer, runs)
            yield PulledMachine(before, release, market, runs)
            return


def _install_working_files(pulled: PulledMachine, *, market: Path, runs: Path | None) -> None:
    """Both verified files are ready before changing either disposable destination."""
    files = [(pulled.market, market)]
    if pulled.runs is not None and runs is not None:
        files.append((pulled.runs, runs))
    with exclusive_lock(market.parent / ".machine-pull.lock", subject="machine pull"):
        previous: dict[Path, Path] = {}
        empty_sidecars: list[Path] = []
        for source, target in files:
            target.parent.mkdir(parents=True, exist_ok=True)
            if target.is_symlink() or (target.exists() and not target.is_file()):
                raise ValueError("machine working destination is not a regular file")
            for suffix in ("-wal", "-journal"):
                sidecar = target.with_name(target.name + suffix)
                with suppress(FileNotFoundError):
                    if sidecar.stat().st_size:
                        raise ValueError(
                            "close local SQLite writers before replacing working files"
                        )
                    empty_sidecars.append(sidecar)
            # Closed read-only SQLite can leave an empty WAL and its index file.
            empty_sidecars.append(target.with_name(target.name + "-shm"))
            if source.stat().st_dev != target.parent.stat().st_dev:
                raise ValueError("machine working destinations must share one filesystem")
            if target.exists():
                prior = source.with_suffix(".prior")
                prior.hardlink_to(target)
                previous[target] = prior
        for sidecar in empty_sidecars:
            sidecar.unlink(missing_ok=True)
        installed: list[Path] = []
        try:
            for source, target in files:
                source.replace(target)
                installed.append(target)
            for target_directory in {target.parent for _, target in files}:
                fsync_directory(target_directory)
        except BaseException:
            for target in reversed(installed):
                if (prior := previous.get(target)) is None:
                    target.unlink()
                else:
                    prior.replace(target)
            raise


def _pull(args: argparse.Namespace) -> dict[str, object]:
    handle = None if args.lease_handle is None else _load_handle(args.lease_handle)
    with (
        closing(Boto3R2Store(bucket=args.bucket)) as store,
        open_lake(mirror=args.mirror, bucket=args.bucket) as (session, cache),
    ):
        if handle is not None:
            verify_held(store, handle)
        with pull_current_machine(
            store,
            cache.source,
            directory=args.market.parent,
            portfolio_only=args.mode == "portfolio",
        ) as pulled:
            hydrated_datasets = 0
            if args.mode != "daily":
                # In the daily workflow economic L1 is allowed after preflight even
                # if heavier Market hydration fails later.
                report = hydrate_release(
                    session,
                    cache,
                    store=pulled.market,
                    release=pulled.release,
                    bucket=args.bucket,
                    mode=args.mode,
                )
                hydrated_datasets = len(report.rows)
            if handle is not None:
                verify_held(store, handle)
            _install_working_files(pulled, market=args.market, runs=args.runs)
            result: dict[str, object] = {
                "mode": args.mode,
                "release_id": pulled.release.release_id,
                "release_manifest_sha256": pulled.release.manifest_sha256,
                "hydrated_datasets": hydrated_datasets,
            }
            if pulled.runs is not None:
                assert pulled.current.pointer is not None
                generation, selection = machine_publication_ids(pulled.current.pointer)
                result.update(generation_id=generation, selection_id=selection)
            return result


def _hydrate_daily(args: argparse.Namespace) -> dict[str, object]:
    """Use the exact embedded origin from the paired pull, under the same daily lease."""
    handle = _load_handle(args.lease_handle)
    origin = read_lake_store_origin(args.market)
    if origin is None:
        raise LakePublishError("daily pulled Market has no fixed Lake origin")
    pointer = LakeReleasePointer(
        release_id=origin.release_id,
        manifest_key=release_manifest_key(release_id=origin.release_id),
        manifest_sha256=origin.release_manifest_sha256,
    )
    with (
        closing(Boto3R2Store(bucket=args.bucket)) as store,
        open_lake(mirror=args.mirror, bucket=args.bucket) as (session, cache),
    ):
        verify_held(store, handle)
        release = resolve_machine_lake(cache.source, pointer)

        def still_working_origin() -> tuple[str, str]:
            verify_held(store, handle)
            require_market_origin(pointer, args.market)
            return origin.release_id, origin.release_manifest_sha256

        return hydrate_release(
            session,
            cache,
            store=args.market,
            release=release,
            bucket=args.bucket,
            mode="daily",
            still_current=still_working_origin,
        ).as_dict()


def main(argv: list[str] | None = None) -> int:
    root = argparse.ArgumentParser(description=__doc__)
    commands = root.add_subparsers(dest="command", required=True)
    parser = commands.add_parser("pull")
    parser.add_argument("--mode", choices=("full", "daily", "portfolio"), required=True)
    parser.add_argument("--runs", type=Path)
    parser.add_argument("--lease-handle", type=Path)
    hydrate = commands.add_parser("hydrate-daily")
    hydrate.add_argument("--lease-handle", type=Path, required=True)
    for command in (parser, hydrate):
        command.add_argument("--market", type=Path, required=True)
        command.add_argument("--mirror", type=Path, required=True)
        command.add_argument("--bucket", required=True)
    args = root.parse_args(argv)
    if args.command == "pull" and (
        (args.mode == "portfolio") != (args.runs is None)
        or (args.mode == "daily") != (args.lease_handle is not None)
        or (args.runs is not None and args.market.resolve() == args.runs.resolve())
    ):
        root.error("invalid working paths or lease for this pull mode")
    try:
        result = _pull(args) if args.command == "pull" else _hydrate_daily(args)
        print(json.dumps(result, sort_keys=True))
    except (Exception, R2OperationStopped) as error:
        # Neither a provider failure nor invalid local files needs private details.
        print(f"machine pull stopped: {type(error).__name__}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
