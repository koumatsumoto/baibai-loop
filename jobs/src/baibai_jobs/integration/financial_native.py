"""Rebuild financial observations from an independent fixed L1 head, offline."""

from __future__ import annotations

import argparse
import os
import sys
from contextlib import closing
from datetime import date, timedelta
from pathlib import Path

import duckdb
import pyarrow.parquet as pq  # type: ignore[import-untyped]

from baibai_engine.data_collection.providers.jquants.financial_native import (
    FinancialL1,
    FixedFinancialL1,
)
from baibai_engine.machine_analysis.observations.financials.from_l1 import (
    materialize_financial_days,
    rebuild_financial_days,
)
from baibai_engine.storage.json import write_json_atomic
from baibai_engine.storage.objects.r2 import Boto3R2Store, ObjectStore


def rebuild(
    store: ObjectStore, fixed: FixedFinancialL1, *, start: date, end: date, output: Path
) -> dict[str, int]:
    if start > end:
        raise ValueError("invalid financial rebuild interval")
    output.mkdir(parents=True, exist_ok=False)
    days = tuple(start + timedelta(days=i) for i in range((end - start).days + 1))
    results = rebuild_financial_days(fixed.requests(store, days=days), days=days)
    # The source head does not assert full provider history. Coverage is only
    # for the explicitly requested days and includes retained failures.
    complete = all(
        item.request_id is not None and not item.incomplete_requests for item in results.values()
    )
    with duckdb.connect(":memory:") as connection:
        materialize_financial_days(
            connection, results.values(), l1_coverage_status="complete" if complete else "partial"
        )
        for table in ("financial_summaries", "financial_receipt_status"):
            pq.write_table(
                connection.table(table).to_arrow_table(),
                output / (table + ".parquet"),
            )
    (output / "head.json").write_bytes(fixed.payload)
    scopes = fixed.value()["scopes"]
    write_json_atomic(
        output / "input.json",
        {
            "head_sha256": fixed.content_id,
            "start": start.isoformat(),
            "end": end.isoformat(),
            "acquisition": {day.isoformat(): scopes.get(day.isoformat()) for day in days},
        },
    )
    return {
        "days": len(days),
        "rows": sum(len(item.rows) for item in results.values()),
        "unavailable": sum(item.status == "unavailable" for item in results.values()),
        "partial": sum(item.status == "partial" for item in results.values()),
        "incomplete": sum(bool(item.incomplete_requests) for item in results.values()),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--start", type=date.fromisoformat, required=True)
    parser.add_argument("--end", type=date.fromisoformat, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--work-directory", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        with closing(
            Boto3R2Store(bucket=os.environ.get("R2_STORES_BUCKET") or "baibai-stores")
        ) as store:
            fixed = FinancialL1(store, args.work_directory).fixed()
            print(rebuild(store, fixed, start=args.start, end=args.end, output=args.output))
    except Exception as error:
        print(f"financial native rebuild failed: {type(error).__name__}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
