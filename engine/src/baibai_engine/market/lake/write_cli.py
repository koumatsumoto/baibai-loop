"""Write-side CLI for the L1 canonical lake."""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Callable
from datetime import UTC, date, datetime
from pathlib import Path

from .datasets import LAKE_DATASETS
from .duck import LakeCredentialError
from .hydrate import LakeHydrateError, dehydrate_market_store, hydrate_market_store
from .identity import source_repo_root, verified_git_commit
from .models import DatasetManifest, L1ReleaseSourceRef, load_lake_model_json
from .objects import LakeObjectError, LakeObjectSource, open_lake
from .projection import ProjectionError, build_projection
from .raw import RawRetentionClass, archive_raw_file, raw_source_ref
from .reader import (
    FixedRelease,
    LakeReadError,
    resolve_current_release,
    resolve_release,
    resolve_release_ref,
)
from .release import create_l1_release
from .retention import LakeRetentionError, apply_gc, plan_gc
from .writer import export_lake_legacy, export_legacy_sqlite, sealed_sqlite_snapshot

_AUDIT_HELP = (
    "re-derive every carried month from SQLite as well as the months this build wrote; "
    "the default proves this build, this proves the whole store"
)

WRITE_COMMANDS = frozenset(
    {
        "archive-raw",
        "dehydrate",
        "export-all",
        "export-legacy",
        "gc",
        "hydrate",
        "projection",
        "release",
    }
)


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="baibai-engine lake")
    commands = parser.add_subparsers(dest="command", required=True)

    raw = commands.add_parser("archive-raw", help="archive original provider bytes append-only")
    raw.add_argument("--source-file", type=Path, required=True)
    raw.add_argument("--mirror", type=Path, required=True)
    raw.add_argument("--provider", choices=("jquants",), default="jquants")
    raw.add_argument("--dataset", choices=sorted(LAKE_DATASETS), required=True)
    raw.add_argument("--ingest-id", required=True)
    raw.add_argument("--suffix", required=True)
    raw.add_argument("--retention", choices=tuple(RawRetentionClass), required=True)
    raw.add_argument("--endpoint")
    raw.add_argument("--from", dest="start", type=date.fromisoformat)
    raw.add_argument("--to", dest="end", type=date.fromisoformat)

    export = commands.add_parser(
        "export-legacy", help="export affected SQLite months as canonical Parquet"
    )
    export.add_argument("--dataset", choices=sorted(LAKE_DATASETS), required=True)
    export.add_argument("--sqlite", type=Path, required=True)
    export.add_argument("--mirror", type=Path, required=True)
    export.add_argument("--from", dest="start", type=date.fromisoformat)
    export.add_argument("--to", dest="end", type=date.fromisoformat)
    export.add_argument("--base-manifest", type=Path)
    export.add_argument("--raw-metadata", type=Path, action="append", default=[])
    export.add_argument("--build-id")
    export.add_argument(
        "--audit",
        action="store_true",
        help=_AUDIT_HELP,
    )

    export_all = commands.add_parser(
        "export-all", help="export every lake dataset from one sealed SQLite snapshot"
    )
    export_all.add_argument("--sqlite", type=Path, required=True)
    export_all.add_argument("--mirror", type=Path, required=True)
    export_all.add_argument("--base-manifest", type=Path, action="append", default=[])
    export_all.add_argument(
        "--audit",
        action="store_true",
        help=_AUDIT_HELP,
    )

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
    _add_release_target(build)
    build.add_argument("--force", action="store_true")

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
    _add_release_target(dehydrate, datasets=False)

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
        verified_commit = _git_commit()
        with sealed_sqlite_snapshot(sqlite_path=args.sqlite, mirror_root=args.mirror) as snapshot:
            report = export_legacy_sqlite(
                dataset_name=args.dataset,
                mirror_root=args.mirror,
                producer_git_commit=verified_commit,
                start=args.start,
                end=args.end,
                base_manifest_path=args.base_manifest,
                raw_source_refs=tuple(raw_source_ref(path) for path in args.raw_metadata),
                source_snapshot=snapshot,
                build_id=args.build_id,
                audit_full_history=args.audit,
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
    if args.command == "export-all":
        bases: dict[str, Path] = {}
        for path in args.base_manifest:
            manifest = load_lake_model_json(path.read_bytes(), DatasetManifest)
            if manifest.dataset in bases:
                raise RuntimeError(f"duplicate base manifest: {manifest.dataset}")
            bases[manifest.dataset] = path
        export_report = export_lake_legacy(
            sqlite_path=args.sqlite,
            mirror_root=args.mirror,
            producer_git_commit=_git_commit(),
            base_manifest_paths=bases,
            audit_full_history=args.audit,
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
    if args.command == "projection":
        return _projection_build(args)
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


def _add_release_target(parser: argparse.ArgumentParser, *, datasets: bool = True) -> None:
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
    if datasets:
        parser.add_argument("--dataset", action="append", default=[], choices=sorted(LAKE_DATASETS))


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
        resolved = resolve_current_release(source, evaluated_at=datetime.now(UTC))
        return (resolved.release_id, resolved.manifest_sha256)

    return resolve_current_release(source, evaluated_at=datetime.now(UTC)), still_current


def _selected_datasets(args: argparse.Namespace, release: FixedRelease) -> tuple[str, ...]:
    """Default to what the release holds, never to what the registry declares.

    A dataset whose source has not started publishing is absent from the release by
    design, and defaulting to the registry would make every such release refuse to
    build until it does. A named dataset the release lacks still fails closed.
    """

    requested = tuple(dict.fromkeys(args.dataset))
    return requested or tuple(sorted(release.dataset_manifests))


def _projection_build(args: argparse.Namespace) -> int:
    """Resolve one release, then materialize it into a disposable SQLite projection."""

    commit = _git_commit()
    try:
        with open_lake(mirror=args.mirror, bucket=args.bucket) as (session, cache):
            release, still_current = _resolve_release_target(args, cache.source)
            report = build_projection(
                session,
                release=release,
                cache=cache,
                destination=args.projection,
                dataset_names=_selected_datasets(args, release),
                builder_git_commit=commit,
                still_current=still_current,
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


def _hydrate(args: argparse.Namespace) -> int:
    """Resolve one release, then fill the market store's lake-owned tables from it."""

    try:
        with open_lake(mirror=args.mirror, bucket=args.bucket) as (session, cache):
            release, still_current = _resolve_release_target(args, cache.source)
            report = hydrate_market_store(
                session,
                release=release,
                cache=cache,
                store=args.store,
                dataset_names=_selected_datasets(args, release),
                still_current=still_current,
            )
    except (
        LakeCredentialError,
        LakeHydrateError,
        LakeObjectError,
        LakeReadError,
        ProjectionError,
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
            release, _ = _resolve_release_target(args, cache.source)
            report = dehydrate_market_store(args.store, release=release)
    except (
        LakeCredentialError,
        LakeHydrateError,
        LakeObjectError,
        LakeReadError,
        ProjectionError,
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
  archive-raw        archive original provider bytes append-only
  export-legacy      export affected SQLite months as canonical Parquet
  export-all         export every lake dataset from one sealed SQLite snapshot
  release create     create an immutable L1 release manifest
  projection build   materialize a local SQLite projection of one fixed release
  hydrate            fill a market SQLite store's lake tables from one fixed release
  dehydrate          empty a market SQLite copy of every row one release holds
  gc                 plan (default) or apply deletion of unreachable objects
"""
    )


def _git_commit() -> str:
    return verified_git_commit()


def _source_repo_root() -> Path:
    return source_repo_root()
