"""Rebuild financial observations from an independent fixed L1 head, offline."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from contextlib import closing
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

import duckdb
import pyarrow.parquet as pq  # type: ignore[import-untyped]

from baibai_engine.data_collection.providers.jquants.financial_native import (
    FinancialL1,
    FixedFinancialL1,
)
from baibai_engine.machine_analysis.generation.contracts import InputSnapshot
from baibai_engine.machine_analysis.generation.inputs import FinancialModelInput
from baibai_engine.machine_analysis.observations.financials.financial_documents import (
    normalize_financial_summary,
)
from baibai_engine.machine_analysis.observations.financials.from_l1 import (
    materialize_financial_days,
    rebuild_financial_days,
)
from baibai_engine.storage.json import write_json_atomic
from baibai_engine.storage.objects.r2 import Boto3R2Store, ObjectStore


def model_input(
    store: ObjectStore, fixed: FixedFinancialL1, *, as_of: date, evaluated_at: datetime
) -> FinancialModelInput:
    """Use retained native documents, with exact membership and acquisition state saved together."""
    if evaluated_at.utcoffset() is None:
        raise ValueError("evaluation time requires a timezone")
    head = fixed.value()
    retained_days = [
        date.fromisoformat(day) for day in head["scopes"] if date.fromisoformat(day) <= as_of
    ]
    if not retained_days:
        raise ValueError("native financial L1 has no retained source days")
    start = min(retained_days)
    days = tuple(start + timedelta(days=i) for i in range((as_of - start).days + 1))
    scopes = {day.isoformat(): head["scopes"].get(day.isoformat(), {}) for day in days}
    for scope in scopes.values():
        for field in ("confirmed_at", "last_attempt_at", "changed_at"):
            stamp = scope.get(field)
            if stamp is not None and datetime.fromisoformat(stamp) > evaluated_at:
                raise ValueError("financial confirmation state is later than the evaluation clock")
    identities = {
        identity
        for scope in scopes.values()
        for key in ("adopted", "failure", "held")
        if (identity := scope.get(key))
    }
    # History and equal-confirmation identities not used for this calculation are
    # deliberately absent. Bodies remain in L1 packs, not in generation metadata.
    selected: dict[str, Any] = {
        "head_sha256": fixed.content_id,
        "as_of": as_of.isoformat(),
        "scopes": scopes,
        "receipts": {identity: head["receipts"][identity] for identity in sorted(identities)},
        "pending_days": [] if head["pending"] is None else head["pending"]["days"],
    }
    requests = tuple(fixed.requests(store, days=days))
    if any(
        item.last_received_at is not None and item.last_received_at > evaluated_at
        for item in requests
    ):
        raise ValueError("financial input contains receipts later than the evaluation clock")
    results = rebuild_financial_days(requests, days=days)
    selected["receipt_status"] = {
        day.isoformat(): {"status": item.status, "incomplete": item.incomplete_requests}
        for day, item in results.items()
    }
    documents = []
    for item in results.values():
        for row in item.rows:
            document = normalize_financial_summary(json.loads(row[3]))
            if document is None:
                raise ValueError("native financial projection contains an ineligible document")
            documents.append(document)
    payload = json.dumps(
        selected, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    ).encode()
    digest = hashlib.sha256(payload).hexdigest()
    return FinancialModelInput(
        InputSnapshot(
            kind="native_l1",
            reference="jquants-financial-" + digest,
            sha256=digest,
            descriptor=selected,
        ),
        tuple(documents),
    )


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


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--start", type=date.fromisoformat, required=True)
    parser.add_argument("--end", type=date.fromisoformat, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--work-directory", type=Path, required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
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
