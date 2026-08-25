"""Write-side CLI for the L1 canonical lake."""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Callable
from pathlib import Path

from ..sqlite.lake_origin import read_lake_store_origin
from .duck import LakeCredentialError
from .hydrate import LakeHydrateError, dehydrate_market_store, hydrate_market_store
from .identity import source_repo_root, verified_git_commit
from .models import L1ReleaseSourceRef, load_lake_model_json
from .objects import LakeObjectError, LakeObjectSource, open_lake
from .prefetch import prefetching_hydration_cache
from .reader import (
    FixedRelease,
    LakeReadError,
    resolve_current_release,
    resolve_release,
    resolve_release_ref,
)
from .release import create_l1_release
from .retention import LakeRetentionError, apply_gc, plan_gc
from .writer import export_lake_legacy

WRITE_COMMANDS = frozenset(
    {
        "dehydrate",
        "export-all",
        "gc",
        "hydrate",
        "release",
    }
)


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="baibai-engine lake")
    commands = parser.add_subparsers(dest="command", required=True)

    export_all = commands.add_parser(
        "export-all", help="export every lake dataset from one sealed SQLite snapshot"
    )
    export_all.add_argument("--sqlite", type=Path, required=True)
    export_all.add_argument("--mirror", type=Path, required=True)

    release = commands.add_parser("release", help="create an immutable L1 release")
    release_commands = release.add_subparsers(dest="release_command", required=True)
    create = release_commands.add_parser("create")
    create.add_argument("--dataset-manifest", type=Path, action="append", required=True)
    create.add_argument("--mirror", type=Path, required=True)
    create.add_argument("--release-id")

    hydrate = commands.add_parser(
        "hydrate", help="fill a market SQLite store's lake tables from one fixed release"
    )
    hydrate.add_argument("--mirror", type=Path, required=True)
    hydrate.add_argument("--store", type=Path, required=True)
    _add_release_target(hydrate)

    dehydrate = commands.add_parser(
        "dehydrate", help="empty a market SQLite copy of every row one release already holds"
    )
    dehydrate.add_argument("--mirror", type=Path, required=True)
    dehydrate.add_argument("--store", type=Path, required=True)
    _add_release_target(dehydrate)

    gc = commands.add_parser("gc", help="plan or apply deletion of unreachable objects")
    gc.add_argument("--mirror", type=Path, required=True)
    gc.add_argument(
        "--apply",
        action="store_true",
        help="delete the planned keys; requires the plan hash the dry run printed",
    )
    gc.add_argument("--plan-hash", help="plan hash from the dry run; required with --apply")

    args = parser.parse_args(argv)
    if args.command == "hydrate":
        return _hydrate(args)
    if args.command == "dehydrate":
        return _dehydrate(args)
    if args.command == "export-all":
        export_report = export_lake_legacy(
            sqlite_path=args.sqlite,
            mirror_root=args.mirror,
            producer_git_commit=_git_commit(),
            expected_store_origin=read_lake_store_origin(args.sqlite),
        )
        print(
            json.dumps(
                {
                    "manifests": {
                        name: str(item.manifest_path)
                        for name, item in export_report.datasets.items()
                    },
                    "snapshot_source_id": export_report.snapshot.source_id,
                    "snapshot_sha256": export_report.snapshot.sha256,
                },
                sort_keys=True,
            )
        )
        return 0
    if args.command == "gc":
        return _gc(args)
    path, release_manifest = create_l1_release(
        dataset_manifest_paths=args.dataset_manifest,
        mirror_root=args.mirror,
        release_id=args.release_id,
    )
    print(
        json.dumps(
            {
                "data_as_of": release_manifest.data_as_of.isoformat(),
                "manifest": str(path),
                "release_id": release_manifest.release_id,
            },
            sort_keys=True,
        )
    )
    return 0


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
        reference = load_lake_model_json(args.release_ref.read_bytes(), L1ReleaseSourceRef)
        return resolve_release_ref(source, reference), None

    def still_current() -> tuple[str, str]:
        resolved = resolve_current_release(source)
        return (resolved.release_id, resolved.manifest_sha256)

    return resolve_current_release(source), still_current


def _hydrate(args: argparse.Namespace) -> int:
    """Resolve one release, then fill the market store's lake-owned tables from it."""

    try:
        with open_lake(mirror=args.mirror, bucket=args.bucket) as (session, cache):
            release, still_current = _resolve_release_target(args, cache.source)
            dataset_names = release.dataset_names()
            with prefetching_hydration_cache(
                cache,
                release=release,
                dataset_names=dataset_names,
                bucket=args.bucket,
            ) as hydration_cache:
                report = hydrate_market_store(
                    session,
                    release=release,
                    cache=hydration_cache,
                    store=args.store,
                    dataset_names=dataset_names,
                    still_current=still_current,
                )
    except (
        LakeCredentialError,
        LakeHydrateError,
        LakeObjectError,
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
            report = dehydrate_market_store(
                args.store,
                release=release,
                still_current=still_current,
            )
    except (
        LakeCredentialError,
        LakeHydrateError,
        LakeObjectError,
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


def print_combined_help() -> None:
    print(
        """usage: baibai-engine lake [-h] COMMAND ...

commands:
  inventory          inspect a local lake mirror without reading object contents
  validate           validate one manifest contract without changing objects
  resolve            resolve one fixed release and print its immutable identity
  export-all         export every lake dataset from one sealed SQLite snapshot
  release create     create an immutable L1 release manifest
  hydrate            fill a market SQLite store's lake tables from one fixed release
  dehydrate          empty a market SQLite copy of every row one release holds
  gc                 plan (default) or apply deletion of unreachable objects
"""
    )


def _git_commit() -> str:
    return verified_git_commit()


def _source_repo_root() -> Path:
    return source_repo_root()
