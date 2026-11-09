"""Resolve explicit master backfill dates, then acquire canonical L1 without an L2 gate."""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from contextlib import closing
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from baibai_engine.data_collection.providers.jquants.http import JQuantsHTTP
from baibai_engine.data_collection.providers.jquants.master_native import MasterL1
from baibai_engine.storage.objects.r2 import Boto3R2Store
from baibai_jobs.acquisition.jquants_master_native import acquire_master_native


def backfill_native_master(args: argparse.Namespace) -> int:
    """Acquire explicit dates without requiring previous L2."""
    try:
        dates = [date.fromisoformat(value) for value in (args.asofs or [])]
        dates = sorted(set(dates))
        if not dates or any(day > datetime.now(ZoneInfo("Asia/Tokyo")).date() for day in dates):
            raise ValueError("master backfill requires explicit nonfuture snapshot dates")
        with (
            tempfile.TemporaryDirectory(prefix="master-native-backfill-") as temporary,
            closing(
                Boto3R2Store(bucket=os.environ.get("R2_STORES_BUCKET") or "baibai-stores")
            ) as store,
            closing(JQuantsHTTP(os.environ.get("JQUANTS_API_KEY", ""))) as client,
        ):
            reports = {}
            for day in dates:
                result = acquire_master_native(
                    MasterL1(store, Path(temporary) / day.isoformat()),
                    start=day,
                    end=day,
                    client=client,
                    force=False,
                )
                reports[day.isoformat()] = result
                if result["halted"] or result["failed"] or result["held"]:
                    break
            print(json.dumps({"independent_l1": reports}, sort_keys=True))
            return int(
                len(reports) != len(dates)
                or any(
                    item["failed"] or item["held"] or item["incomplete"]
                    for item in reports.values()
                )
            )
    except Exception as error:
        print(f"native master backfill failed: {type(error).__name__}", file=sys.stderr)
        return 1
