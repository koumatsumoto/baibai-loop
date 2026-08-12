"""Write-side CLI for the concrete Phase 1 lake pilot."""

from __future__ import annotations

import argparse
import json
import subprocess  # nosec B404
import sys
from datetime import date
from pathlib import Path

from .datasets import PILOT_DATASETS
from .duck import LakeCredentialError
from .objects import LakeObjectError, open_lake
from .projection import ProjectionError, build_projection
from .raw import RawRetentionClass, archive_raw_file
from .reader import LakeReadError, resolve_current_release, resolve_release
from .release import create_l1_release
from .retention import LakeRetentionError, apply_gc, create_pin, plan_gc, remove_pin
from .writer import export_legacy_sqlite, validate_legacy_parity

WRITE_COMMANDS = frozenset({"archive-raw", "export-legacy", "gc", "pin", "projection", "release"})


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="baibai-engine lake")
    commands = parser.add_subparsers(dest="command", required=True)

    raw = commands.add_parser("archive-raw", help="archive original provider bytes append-only")
    raw.add_argument("--source-file", type=Path, required=True)
    raw.add_argument("--mirror", type=Path, required=True)
    raw.add_argument("--provider", choices=("jquants",), default="jquants")
    raw.add_argument("--dataset", choices=sorted(PILOT_DATASETS), required=True)
    raw.add_argument("--ingest-id", required=True)
    raw.add_argument("--suffix", required=True)
    raw.add_argument("--retention", choices=tuple(RawRetentionClass), required=True)
    raw.add_argument("--endpoint")
    raw.add_argument("--from", dest="start", type=date.fromisoformat)
    raw.add_argument("--to", dest="end", type=date.fromisoformat)

    export = commands.add_parser(
        "export-legacy", help="export affected SQLite months as canonical Parquet"
    )
    export.add_argument("--dataset", choices=sorted(PILOT_DATASETS), required=True)
    export.add_argument("--sqlite", type=Path, required=True)
    export.add_argument("--mirror", type=Path, required=True)
    export.add_argument("--from", dest="start", type=date.fromisoformat)
    export.add_argument("--to", dest="end", type=date.fromisoformat)
    export.add_argument("--base-manifest", type=Path)
    export.add_argument("--source-ingest", action="append", default=[])
    export.add_argument("--build-id")
    export.add_argument("--producer-git-commit", default=None)

    release = commands.add_parser("release", help="create an immutable L1 release")
    release_commands = release.add_subparsers(dest="release_command", required=True)
    create = release_commands.add_parser("create")
    create.add_argument("--dataset-manifest", type=Path, action="append", required=True)
    create.add_argument("--mirror", type=Path, required=True)
    create.add_argument("--release-id")

    projection = commands.add_parser(
        "projection", help="materialize a local SQLite projection of one fixed release"
    )
    projection_commands = projection.add_subparsers(dest="projection_command", required=True)
    build = projection_commands.add_parser("build")
    build.add_argument("--mirror", type=Path, required=True)
    build.add_argument("--projection", type=Path, required=True)
    build.add_argument(
        "--bucket",
        help="fetch missing objects from this R2 bucket; omit to build from the mirror alone",
    )
    build.add_argument(
        "--release",
        help="build this release instead of the one the current pointer names",
    )
    build.add_argument("--dataset", action="append", default=[], choices=sorted(PILOT_DATASETS))
    build.add_argument("--force", action="store_true")
    build.add_argument("--producer-git-commit", default=None)

    pin = commands.add_parser("pin", help="keep one release or build reachable indefinitely")
    pin_commands = pin.add_subparsers(dest="pin_command", required=True)
    pin_create = pin_commands.add_parser("create")
    pin_create.add_argument("--mirror", type=Path, required=True)
    pin_create.add_argument("--pin-id", required=True)
    pin_create.add_argument("--release", help="L1 release id to pin")
    pin_create.add_argument("--build", help="L2 build id to pin (requires --dataset)")
    pin_create.add_argument("--dataset")
    pin_create.add_argument("--reason", required=True)
    pin_create.add_argument("--owner", required=True)
    pin_remove = pin_commands.add_parser("remove")
    pin_remove.add_argument("--mirror", type=Path, required=True)
    pin_remove.add_argument("--pin-id", required=True)

    gc = commands.add_parser("gc", help="plan or apply deletion of unreachable objects")
    gc.add_argument("--mirror", type=Path, required=True)
    gc.add_argument(
        "--l2-dataset",
        action="append",
        default=[],
        help=(
            "require this L2 dataset to have a current pointer; roots are read from "
            "the store, so naming datasets is an assertion rather than the root list"
        ),
    )
    gc.add_argument(
        "--apply",
        action="store_true",
        help="delete the planned keys; requires the plan hash the dry run printed",
    )
    gc.add_argument("--plan-hash", help="plan hash from the dry run; required with --apply")

    args = parser.parse_args(argv)
    if args.command == "archive-raw":
        target, metadata_path, metadata = archive_raw_file(
            source_path=args.source_file,
            mirror_root=args.mirror,
            provider=args.provider,
            dataset=args.dataset,
            ingest_id=args.ingest_id,
            suffix=args.suffix,
            retention_class=RawRetentionClass(args.retention),
            endpoint=args.endpoint,
            request_start=args.start,
            request_end=args.end,
        )
        print(
            json.dumps(
                {
                    "bytes": metadata.bytes,
                    "metadata": str(metadata_path),
                    "object": str(target),
                    "sha256": metadata.content_sha256,
                },
                sort_keys=True,
            )
        )
        return 0
    if args.command == "export-legacy":
        report = export_legacy_sqlite(
            dataset_name=args.dataset,
            sqlite_path=args.sqlite,
            mirror_root=args.mirror,
            producer_git_commit=args.producer_git_commit or _git_commit(),
            start=args.start,
            end=args.end,
            base_manifest_path=args.base_manifest,
            source_ingest_ids=args.source_ingest,
            build_id=args.build_id,
        )
        selected = None
        if args.start is not None and args.end is not None:
            selected = {
                (year, month)
                for year in range(args.start.year, args.end.year + 1)
                for month in range(1, 13)
                if (year, month) >= (args.start.year, args.start.month)
                and (year, month) <= (args.end.year, args.end.month)
            }
        validate_legacy_parity(
            sqlite_path=args.sqlite,
            mirror_root=args.mirror,
            manifest=report.manifest,
            months=selected,
        )
        print(
            json.dumps(
                {
                    "build_id": report.manifest.build_id,
                    "changed_partitions": report.changed_partitions,
                    "created_objects": report.created_objects,
                    "manifest": str(report.manifest_path),
                    "reused_partitions": report.reused_partitions,
                    "rows": report.manifest.totals.rows,
                },
                sort_keys=True,
            )
        )
        return 0
    if args.command == "projection":
        return _projection_build(args)
    if args.command == "pin":
        return _pin(args)
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


def _projection_build(args: argparse.Namespace) -> int:
    """Resolve one release, then materialize it into a disposable SQLite projection."""

    datasets = tuple(dict.fromkeys(args.dataset)) or tuple(sorted(PILOT_DATASETS))
    commit = args.producer_git_commit or _git_commit()
    try:
        with open_lake(mirror=args.mirror, bucket=args.bucket) as (session, cache):
            release = (
                resolve_release(cache.source, args.release)
                if args.release is not None
                else resolve_current_release(cache.source)
            )
            report = build_projection(
                session,
                release=release,
                cache=cache,
                destination=args.projection,
                dataset_names=datasets,
                producer_git_commit=commit,
                force=args.force,
            )
    except (
        LakeCredentialError,
        LakeObjectError,
        LakeReadError,
        ProjectionError,
        OSError,
        ValueError,
    ) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    print(
        json.dumps(
            {
                "built_at": report.built_at.isoformat(),
                "data_as_of": report.identity.data_as_of.isoformat(),
                "objects": len(report.identity.objects),
                "projection": str(report.path),
                "release_id": report.identity.source_release_id,
                "reused": report.reused,
                "rows": dict(sorted(report.rows.items())),
                **report.transfers.as_dict(),
            },
            sort_keys=True,
        )
    )
    return 0


def _pin(args: argparse.Namespace) -> int:
    """Create or remove one explicit retention root."""

    try:
        if args.pin_command == "remove":
            removed = remove_pin(args.mirror, pin_id=args.pin_id)
            print(json.dumps({"pin_id": args.pin_id, "removed": removed}, sort_keys=True))
            return 0 if removed else 1
        if bool(args.release) == bool(args.build):
            print("error: pin exactly one of --release or --build", file=sys.stderr)
            return 1
        path = create_pin(
            args.mirror,
            pin_id=args.pin_id,
            target_kind="l1_release" if args.release else "l2_build",
            target_id=args.release or args.build,
            dataset=args.dataset if args.build else None,
            reason=args.reason,
            owner=args.owner,
        )
    except (LakeObjectError, LakeRetentionError, OSError, ValueError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    print(json.dumps({"pin": str(path), "pin_id": args.pin_id}, sort_keys=True))
    return 0


def _gc(args: argparse.Namespace) -> int:
    """Plan deletion from the root closure; delete only against that same plan."""

    try:
        plan = plan_gc(args.mirror, l2_datasets=tuple(args.l2_dataset))
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
  archive-raw        archive original provider bytes append-only
  export-legacy      export affected SQLite months as canonical Parquet
  release create     create an immutable L1 release manifest
  projection build   materialize a local SQLite projection of one fixed release
  pin create         keep one release or build reachable indefinitely
  pin remove         drop one explicit retention root
  gc                 plan (default) or apply deletion of unreachable objects
"""
    )


def _git_commit() -> str:
    result = subprocess.run(
        ("git", "rev-parse", "HEAD"),
        check=True,
        capture_output=True,
        text=True,
    )  # nosec B603
    return result.stdout.strip()
