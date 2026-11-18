"""Write-side CLI for the market lake."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from collections.abc import Callable
from contextlib import closing
from pathlib import Path

from baibai_engine.storage.objects.r2 import LakePublishError
from baibai_engine.storage.snapshots.hydrate import (
    LakeHydrateError,
    dehydrate_market_store,
    hydrate_market_store,
)
from baibai_engine.storage.snapshots.models import LakeReleaseSourceRef, load_lake_model_json
from baibai_engine.storage.snapshots.objects import (
    MAX_BUFFERED_OBJECT_BYTES,
    LakeCredentialError,
    LakeObjectError,
    LakeObjectSource,
    open_lake,
)
from baibai_engine.storage.snapshots.prefetch import prefetching_hydration_cache
from baibai_engine.storage.snapshots.reader import (
    FixedRelease,
    LakeReadError,
    resolve_current_release,
    resolve_release,
    resolve_release_ref,
)
from baibai_engine.storage.snapshots.retention import LakeRetentionError, apply_gc, plan_gc
from baibai_engine.storage.snapshots.transfer import make_market_transfer
from baibai_engine.storage.sqlite.source_cache.read import connect_read_only
from baibai_jobs.publication.datasets import LAKE_DATASETS
from baibai_jobs.publication.policy import release_policy_for_profile

WRITE_COMMANDS = frozenset(
    {
        "dehydrate",
        "gc",
        "hydrate",
        "hydrate-portfolio",
    }
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="baibai-jobs snapshots")
    commands = parser.add_subparsers(dest="command", required=True)

    add_write_commands(commands)

    return parser


def add_write_commands(commands: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    for command, help_text in (
        ("hydrate", "fill a market SQLite store's lake tables from one fixed release"),
        ("hydrate-portfolio", "fill only portfolio display inputs inside the store's release"),
    ):
        hydrate = commands.add_parser(command, help=help_text)
        hydrate.add_argument("--mirror", type=Path, required=True)
        hydrate.add_argument("--store", type=Path, required=True)
        _add_release_target(hydrate)

    dehydrate = commands.add_parser(
        "dehydrate", help="empty a market SQLite copy of every row one release already holds"
    )
    dehydrate.add_argument("--mirror", type=Path, required=True)
    dehydrate.add_argument("--store", type=Path, required=True)
    dehydrate.add_argument(
        "--source", type=Path, help="build this transfer copy from the working store"
    )
    _add_release_target(dehydrate)

    gc = commands.add_parser("gc", help="plan or apply deletion of unreachable objects")
    gc.add_argument("--mirror", type=Path, required=True)
    gc.add_argument(
        "--apply",
        action="store_true",
        help="delete the planned keys; requires the plan hash the dry run printed",
    )
    gc.add_argument("--plan-hash", help="plan hash from the dry run; required with --apply")


def main(argv: list[str]) -> int:
    args = build_parser().parse_args(argv)
    if args.command in {"hydrate", "hydrate-portfolio"}:
        return _hydrate(args)
    if args.command == "dehydrate":
        return _dehydrate(args)
    return _gc(args)


def _add_release_target(parser: argparse.ArgumentParser) -> None:
    """The three ways to name one fixed release, shared by every build that reads one."""

    parser.add_argument(
        "--bucket",
        help="fetch missing objects from this R2 bucket; omit to build from the mirror alone",
    )
    target = parser.add_mutually_exclusive_group()
    target.add_argument(
        "--release",
        help="build this release instead of the one the current pointer names",
    )
    target.add_argument(
        "--release-ref", type=Path, help="build a typed digest-pinned release reference"
    )
    parser.add_argument("--manifest-sha256", help="required digest when --release is used")


def _resolve_release_target(
    args: argparse.Namespace, source: LakeObjectSource
) -> tuple[FixedRelease, Callable[[], tuple[str, str]] | None]:
    """Resolve the named release, and say whether the caller asked for "current".

    Only a caller that asked for current gets a re-check: it resolved the pointer
    before the destination lock, so the pointer may move while it waits and finishing
    last must not undo a newer generation. A caller that named a release means what it
    said and is not re-checked.
    """

    if args.release is not None:
        if args.manifest_sha256 is None:
            raise LakeReadError("--release requires --manifest-sha256")
        return resolve_release(source, args.release, manifest_sha256=args.manifest_sha256), None
    if args.manifest_sha256 is not None:
        raise LakeReadError("--manifest-sha256 is valid only with --release")
    if args.release_ref is not None:
        reference = load_lake_model_json(args.release_ref.read_bytes(), LakeReleaseSourceRef)
        return resolve_release_ref(source, reference), None

    def still_current() -> tuple[str, str]:
        resolved = resolve_current_release(
            source, catalog=LAKE_DATASETS, policy=release_policy_for_profile("production")
        )
        return (resolved.release_id, resolved.manifest_sha256)

    return resolve_current_release(
        source, catalog=LAKE_DATASETS, policy=release_policy_for_profile("production")
    ), still_current


def _clear_independent_list_cache(
    args: argparse.Namespace, release: FixedRelease, *, store_path: Path | None = None
) -> None:
    path = args.store if store_path is None else store_path
    if "edinet.list_responses" in release.dataset_names() or not path.exists():
        return
    with closing(connect_read_only(path)) as connection:
        if connection.execute("SELECT 1 FROM edinet_list_responses LIMIT 1").fetchone() is None:
            return
    from baibai_engine.data_collection.providers.edinet.list_native import EdinetListL1
    from baibai_engine.storage.objects.r2 import Boto3R2Store
    from baibai_jobs.integration.edinet_list_native import clear_verified_receipt_cache

    if args.bucket is None:
        raise ValueError("native EDINET receipt cache verification requires --bucket")
    with closing(Boto3R2Store(bucket=args.bucket)) as store:
        fixed = EdinetListL1(store, args.mirror / "edinet-native").freeze()
        clear_verified_receipt_cache(fixed, path)


def _publication_web_inputs(release: FixedRelease, *, disk_free: int) -> frozenset[str]:
    # The hosted daily runner will publish into this same mirror. Do not fetch a
    # single extra object. Leave at least 15/16 of its free space for SQLite,
    # the seal and other datasets; never carry more than 3 GiB between stages.
    manifest = release.dataset_manifests.get("equity_web.source_responses")
    if manifest is None:
        return frozenset()
    budget = min(3 * 1024**3, disk_free // 16)
    keys = set()
    for part in manifest.partitions:
        for obj in part.objects:
            if MAX_BUFFERED_OBJECT_BYTES < obj.bytes <= budget:
                keys.add(obj.key)
                budget -= obj.bytes
    return frozenset(keys)


def _hydrate(args: argparse.Namespace) -> int:
    """Resolve one release, then fill the market store's lake-owned tables from it."""

    try:
        with open_lake(mirror=args.mirror, bucket=args.bucket) as (session, cache):
            release, still_current = _resolve_release_target(args, cache.source)
            if args.command == "hydrate-portfolio":
                # Saved proposals/outcomes live in App. Only valuation and names need
                # market rows; unrelated native receipt caches are not ours to clear.
                dataset_names: tuple[str, ...] = (
                    "jquants.daily_bars",
                    "jquants.market_calendar",
                    "jquants.master_snapshots",
                )
            else:
                _clear_independent_list_cache(args, release)
                dataset_names = release.dataset_names()
            with prefetching_hydration_cache(
                cache,
                release=release,
                dataset_names=dataset_names,
                bucket=args.bucket,
            ) as hydration_cache:
                if (
                    args.command == "hydrate"
                    and hydration_cache.evict_large_after_read
                    and hydration_cache.root.is_dir()
                    and os.environ.get("BAIBAI_REUSE_DAILY_WEB_INPUTS") == "1"
                ):
                    hydration_cache.keep_for_export = _publication_web_inputs(
                        release, disk_free=shutil.disk_usage(hydration_cache.root).free
                    )
                report = hydrate_market_store(
                    session,
                    release=release,
                    cache=hydration_cache,
                    store=args.store,
                    dataset_names=dataset_names,
                    still_current=still_current,
                    catalog=LAKE_DATASETS,
                )
    except (
        LakeCredentialError,
        LakeHydrateError,
        LakeObjectError,
        LakePublishError,
        LakeReadError,
        OSError,
        ValueError,
    ) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    print(json.dumps(report.as_dict(), sort_keys=True))
    return 0


def _dehydrate(args: argparse.Namespace) -> int:
    """Empty a market SQLite copy of every row the named release already publishes."""

    try:
        with open_lake(mirror=args.mirror, bucket=args.bucket) as (_, cache):
            release, still_current = _resolve_release_target(args, cache.source)
            if args.source is None:
                _clear_independent_list_cache(args, release)
                report = dehydrate_market_store(
                    args.store, release=release, still_current=still_current, catalog=LAKE_DATASETS
                )
            else:
                report = make_market_transfer(
                    args.source,
                    args.store,
                    release=release,
                    still_current=still_current,
                    catalog=LAKE_DATASETS,
                    prepare_copy=lambda path: _clear_independent_list_cache(
                        args, release, store_path=path
                    ),
                )
    except (
        LakeCredentialError,
        LakeHydrateError,
        LakeObjectError,
        LakePublishError,
        LakeReadError,
        OSError,
        ValueError,
    ) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    print(json.dumps(report.as_dict(), sort_keys=True))
    return 0


def _gc(args: argparse.Namespace) -> int:
    """Plan deletion from the root closure; delete only against that same plan."""

    try:
        plan = plan_gc(args.mirror)
        if args.apply:
            if not args.plan_hash:
                print("error: --apply requires --plan-hash from the dry run", file=sys.stderr)
                return 1
            deleted = apply_gc(args.mirror, plan, plan_hash=args.plan_hash)
            print(
                json.dumps({"deleted": list(deleted), "plan_hash": plan.plan_hash}, sort_keys=True)
            )
            return 0
    except (LakeObjectError, LakeRetentionError, OSError, ValueError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    print(json.dumps(plan.as_dict(), sort_keys=True))
    return 0
