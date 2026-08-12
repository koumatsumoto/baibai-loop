"""Write-side CLI for the concrete Phase 1 lake pilot."""

from __future__ import annotations

import argparse
import json
import subprocess  # nosec B404
from datetime import date
from pathlib import Path

from .datasets import PILOT_DATASETS
from .raw import RawRetentionClass, archive_raw_file
from .release import create_l1_release
from .writer import export_legacy_sqlite, validate_legacy_parity

WRITE_COMMANDS = frozenset({"archive-raw", "export-legacy", "release"})


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


def print_combined_help() -> None:
    print(
        """usage: baibai-engine lake [-h] COMMAND ...

commands:
  inventory       inspect a local lake mirror without reading object contents
  validate        validate one manifest contract without changing objects
  archive-raw     archive original provider bytes append-only
  export-legacy   export affected SQLite months as canonical Parquet
  release create  create an immutable L1 release manifest
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
