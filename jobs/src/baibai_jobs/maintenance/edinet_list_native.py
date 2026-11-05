"""Explicit commands for independent EDINET list acquisition and failed work."""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from contextlib import closing
from datetime import date, datetime, timedelta
from pathlib import Path

from baibai_engine.data_collection.providers.edinet.http import EDINETHTTP
from baibai_engine.data_collection.providers.edinet.list_native import (
    EdinetListL1,
    NativeListReceipt,
)
from baibai_engine.storage.objects.r2 import Boto3R2Store
from baibai_jobs.acquisition.edinet_list_native import acquire_lists
from baibai_jobs.acquisition.receipts import (
    ReceiptStore,
    available_handoffs,
    download_fixed,
    seal_integrated,
)


def commit_stage(owner: EdinetListL1, store: ReceiptStore, stage_id: str) -> dict[str, object]:
    entries = [
        entry
        for entry in available_handoffs(store, source="edinet-list", include_integrated=True)
        if entry["stage_id"] == stage_id
    ]
    if len(entries) != 1 or entries[0].get("invalid"):
        raise ValueError("native EDINET failure stage is missing or invalid")
    entry = entries[0]
    packet = owner.directory / "stage"
    download_fixed(store, entry, packet)
    report = json.loads((packet / "result.json").read_bytes())
    if report.get("source") != "edinet-list" or report.get("native_l1_pending") is not True:
        raise ValueError("legacy EDINET stage requires finite cutover, not commit-stage")
    with closing(
        sqlite3.connect((packet / "receipts.sqlite").resolve().as_uri() + "?mode=ro", uri=True)
    ) as conn:
        receipts = [
            NativeListReceipt(
                str(row[0]),
                date.fromisoformat(row[1]),
                datetime.fromisoformat(row[2]),
                bytes(row[3]),
            )
            for row in conn.execute("SELECT * FROM edinet_list_responses")
        ]
    if sorted(report.get("list_receipts", [])) != sorted(item.receipt_id for item in receipts):
        raise ValueError("native failure stage declarations differ from originals")
    owner.freeze()
    fixed = owner.publish(receipts)
    seal_integrated(
        store, entry, generations={"edinet_list_head_sha256": fixed.identity}, recovery=False
    )
    return {
        "status": "committed",
        "stage_id": stage_id,
        "receipt_count": len(receipts),
        "l1_identity": fixed.identity,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("initialize", "acquire", "commit-work", "commit-stage"))
    parser.add_argument("--directory", type=Path, required=True)
    parser.add_argument("--stage-id")
    parser.add_argument("--start", type=date.fromisoformat)
    parser.add_argument("--asof", type=date.fromisoformat)
    parser.add_argument("--force-history", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        with closing(
            Boto3R2Store(bucket=os.environ.get("R2_STORES_BUCKET") or "baibai-stores")
        ) as store:
            owner = EdinetListL1(store, args.directory)
            if args.command == "initialize":
                owner.freeze(require_existing=False)
                fixed = owner.publish([])
                print(json.dumps({"l1_identity": fixed.identity}))
                return 0
            if args.command == "commit-stage":
                if args.stage_id is None:
                    raise ValueError("--stage-id is required")
                print(json.dumps(commit_stage(owner, store, args.stage_id), sort_keys=True))
                return 0
            if args.command == "commit-work":
                fixed = owner.resume_work()
                print(json.dumps({"l1_identity": fixed.identity}))
                return 0
            if args.asof is None:
                raise ValueError("--asof is required")
            start = args.start if args.start is not None else args.asof - timedelta(days=540)
            with closing(EDINETHTTP(os.environ.get("EDINET_API_KEY", ""))) as client:
                report = acquire_lists(
                    owner,
                    client,
                    start=start,
                    end=args.asof,
                    force_history=args.force_history,
                )
            print(json.dumps(report, sort_keys=True))
            return int(bool(report["failed"]))
    except Exception as error:
        print(f"EDINET list L1 failed: {type(error).__name__}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
