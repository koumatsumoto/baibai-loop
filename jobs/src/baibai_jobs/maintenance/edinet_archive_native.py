"""独立archive/scopeの明示取得。通常workflowや旧marketの切替は行わない。"""

from __future__ import annotations

import argparse
import json
import os
import sys
from contextlib import closing
from datetime import date
from pathlib import Path

from baibai_engine.data_collection.providers.edinet.archive_native import EdinetArchiveL1
from baibai_engine.data_collection.providers.edinet.http import EDINETHTTP
from baibai_engine.data_collection.providers.edinet.list_native import EdinetListL1
from baibai_engine.storage.json import write_json_atomic
from baibai_engine.storage.objects.r2 import Boto3R2Store
from baibai_engine.storage.snapshots.objects import open_lake
from baibai_engine.storage.snapshots.reader import resolve_release
from baibai_jobs.acquisition.edinet_archive_native import acquire_archives
from baibai_jobs.maintenance.edinet_archive_import import (
    apply_import,
    prepare_import,
    verify_import,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "command",
        choices=(
            "initialize",
            "acquire",
            "freeze",
            "commit-work",
            "prepare-import",
            "apply-import",
            "verify-import",
        ),
    )
    parser.add_argument("--directory", type=Path, required=True)
    parser.add_argument("--bucket", required=True)
    parser.add_argument("--group", choices=("edinet-metrics", "edinet-facts"))
    parser.add_argument("--asof", type=date.fromisoformat)
    parser.add_argument("--lookback-days", type=int, default=540)
    parser.add_argument("--initialize-facts", action="store_true")
    parser.add_argument("--ticker", action="append", default=[])
    parser.add_argument("--legacy-database", type=Path)
    parser.add_argument("--release")
    parser.add_argument("--manifest-sha256")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "acquire" and (args.asof is None or args.group is None):
            raise ValueError("acquire requires --group and --asof")
        if args.command == "prepare-import" and (
            args.legacy_database is None or args.release is None or args.manifest_sha256 is None
        ):
            raise ValueError(
                "prepare-import requires --legacy-database, --release and --manifest-sha256"
            )
        with closing(Boto3R2Store(bucket=args.bucket)) as store:
            owner = EdinetArchiveL1(store, args.directory)
            if args.command == "prepare-import":
                # Metadata only: no archive partitions or provider HTTP are fetched here.
                with open_lake(
                    mirror=args.directory.parent / "lake-metadata", bucket=args.bucket
                ) as (_, cache):
                    release = resolve_release(
                        cache.source, args.release, manifest_sha256=args.manifest_sha256
                    )
                    plan = prepare_import(
                        store,
                        source=args.legacy_database,
                        release=release,
                        directory=args.directory,
                    )
                print(
                    json.dumps(
                        {"archives": len(plan["archives"]), "databases": len(plan["databases"])}
                    )
                )
                return 0
            if args.command == "apply-import":
                print(json.dumps(apply_import(owner, directory=args.directory), sort_keys=True))
                return 0
            if args.command == "verify-import":
                verify_import(owner, directory=args.directory)
                return 0
            if args.command == "acquire":
                lists = EdinetListL1(store, args.directory / "lists").freeze()
                with closing(EDINETHTTP(os.environ.get("EDINET_API_KEY", ""))) as client:
                    report = acquire_archives(
                        owner,
                        client,
                        lists,
                        group=args.group,
                        asof=args.asof,
                        lookback_days=args.lookback_days,
                        initialize_facts=args.initialize_facts,
                        tickers=tuple(args.ticker),
                    )
                print(json.dumps(report, sort_keys=True))
                return int(report["status"] == "failed")
            fixed = (
                owner.initialize()
                if args.command == "initialize"
                else owner.resume_work()
                if args.command == "commit-work"
                else owner.freeze()
            )
            args.directory.mkdir(parents=True, exist_ok=True)
            write_json_atomic(args.directory / "archive-head.json", fixed.value())
            print(json.dumps({"l1_identity": fixed.identity, "archives": len(fixed.archive_keys)}))
            return 0
    except Exception as error:
        print(f"EDINET archive L1 failed: {type(error).__name__}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
