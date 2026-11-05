"""Reconstruct prices from one fixed native selection, never a previous L2."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import sys
from collections import defaultdict
from collections.abc import Iterator, Sequence
from contextlib import closing
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

import duckdb
import pyarrow.parquet as pq  # type: ignore[import-untyped]
from pydantic import JsonValue

from baibai_engine.data_collection.providers.jquants.price_native import (
    FixedPriceL1,
    PriceL1,
    read_price_requests,
)
from baibai_engine.machine_analysis.generation.contracts import InputSnapshot
from baibai_engine.machine_analysis.generation.price_inputs import (
    read_price_input,
    retain_price_input,
)
from baibai_engine.machine_analysis.observations.prices.from_l1 import (
    PriceDay,
    materialize_price_days,
    rebuild_price_days,
)
from baibai_engine.primitives.json import canonical_json
from baibai_engine.storage.json import write_json_atomic
from baibai_engine.storage.objects.r2 import Boto3R2Store, ObjectStore
from baibai_engine.storage.sqlite.source_cache.coverage import record_source_coverage


def _days(start: date, end: date) -> tuple[date, ...]:
    if start > end:
        raise ValueError("invalid price rebuild interval")
    return tuple(start + timedelta(days=i) for i in range((end - start).days + 1))


def _months(days: Sequence[date]) -> Iterator[tuple[date, ...]]:
    months: dict[str, list[date]] = defaultdict(list)
    for day in sorted(days):
        months[day.strftime("%Y-%m")].append(day)
    yield from (tuple(group) for group in months.values())


def _unresolved(item: PriceDay, scope: dict[str, Any]) -> bool:
    return bool(
        item.status in {"unavailable", "partial"}
        or item.incomplete_requests
        or scope.get("failure")
        or scope.get("held")
    )


def model_input(
    store: ObjectStore,
    fixed: FixedPriceL1,
    *,
    as_of: date,
    evaluated_at: datetime,
    market: Path,
) -> InputSnapshot:
    """Replace the disposable SQLite projection and retain its exact native identity."""
    if evaluated_at.utcoffset() is None:
        raise ValueError("evaluation time requires a timezone")
    head = fixed.value()
    retained = [date.fromisoformat(day) for day in head["scopes"]]
    if not retained:
        raise ValueError("native price L1 has no retained source days")
    days = _days(min(retained), max(as_of, max(retained)))
    scopes = {day.isoformat(): head["scopes"].get(day.isoformat(), {}) for day in days}
    for scope in scopes.values():
        for field in ("confirmed_at", "last_attempt_at", "changed_at"):
            if (stamp := scope.get(field)) and datetime.fromisoformat(stamp) > evaluated_at:
                raise ValueError("price confirmation state is later than the evaluation clock")
    identities = {
        identity
        for scope in scopes.values()
        for field in ("adopted", "failure", "held")
        if (identity := scope.get(field))
    }
    selected = {
        "head_sha256": fixed.content_id,
        "as_of": as_of.isoformat(),
        "scopes": scopes,
        "receipts": {identity: head["receipts"][identity] for identity in sorted(identities)},
    }
    summary: dict[str, Any] = {
        "head_sha256": fixed.content_id,
        "retained_source_days": {
            "start": min(retained).isoformat(),
            "end": max(retained).isoformat(),
        },
        "selection_start": days[0].isoformat(),
        "selection_end": days[-1].isoformat(),
        "as_of": as_of.isoformat(),
        "adopted_requests": sum(bool(s.get("adopted")) for s in scopes.values()),
        "normal_empty_days": 0,
        "unavailable_days": 0,
        "partial_days": 0,
        "incomplete_days": 0,
        "failed_days": sum(bool(s.get("failure")) for s in scopes.values()),
        "held_days": sum(bool(s.get("held")) for s in scopes.values()),
    }
    ranges: list[dict[str, str]] = []
    with duckdb.connect(":memory:") as projection:
        for index, month in enumerate(_months(days)):
            requests = tuple(fixed.requests(store, days=month))
            if any(
                item.last_received_at is not None and item.last_received_at > evaluated_at
                for item in requests
            ):
                raise ValueError("price input contains receipts later than the evaluation clock")
            results = rebuild_price_days(requests, days=month)
            materialize_price_days(
                projection, results.values(), l1_coverage_status="partial", append=index > 0
            )
            for day, item in results.items():
                unresolved = _unresolved(item, scopes[day.isoformat()])
                summary["normal_empty_days"] += int(
                    not unresolved
                    and not item.rows
                    and item.request_id is not None
                    and not item.rejected_count
                    and not item.excluded_count
                )
                summary["unavailable_days"] += int(item.status == "unavailable")
                summary["partial_days"] += int(item.status == "partial")
                summary["incomplete_days"] += int(bool(item.incomplete_requests))
                if unresolved:
                    if ranges and date.fromisoformat(ranges[-1]["end"]) + timedelta(days=1) == day:
                        ranges[-1]["end"] = day.isoformat()
                    else:
                        ranges.append({"start": day.isoformat(), "end": day.isoformat()})
        for name, field, operation in (
            ("oldest_confirmed_at", "confirmed_at", min),
            ("latest_confirmed_at", "confirmed_at", max),
            ("latest_changed_at", "changed_at", max),
        ):
            stamps = [s[field] for s in scopes.values() if s.get(field)]
            summary[name] = operation(stamps) if stamps else None
        summary["unresolved_ranges"] = ranges[:12]
        summary["additional_unresolved_ranges"] = max(0, len(ranges) - 12)
        summary["coverage_status"] = "partial" if ranges else "complete"
        selected["summary"] = summary
        descriptor = retain_price_input(store, selected)
        digest = hashlib.sha256(canonical_json(descriptor).encode()).hexdigest()
        identity = InputSnapshot(
            kind="native_l1",
            reference="jquants-price-" + digest,
            sha256=digest,
            descriptor=descriptor,
        )
        # Validate all native input and retain membership before replacing anything.
        with sqlite3.connect(market) as connection:
            connection.execute("DELETE FROM jquants_daily_bars")
            connection.execute("DELETE FROM jquants_source_pages WHERE source='daily_bars'")
            connection.execute("DELETE FROM source_coverage WHERE source='jquants_daily_bars'")
            cursor = projection.execute("SELECT * FROM daily_bars ORDER BY traded_at,ticker")
            while rows := cursor.fetchmany(10000):
                connection.executemany(
                    "INSERT INTO jquants_daily_bars VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)", rows
                )
            row_counts = dict(
                projection.execute(
                    "SELECT traded_at,COUNT(*) FROM daily_bars GROUP BY traded_at"
                ).fetchall()
            )
            for day, _request_id, status, _, _rejected, _excluded, incomplete in projection.execute(
                "SELECT * FROM price_receipt_status ORDER BY day"
            ).fetchall():
                scope = scopes[day.isoformat()]
                failed = bool(scope.get("failure") or scope.get("held") or incomplete)
                record_source_coverage(
                    connection,
                    source="jquants_daily_bars",
                    coverage_key="native:" + day.isoformat(),
                    coverage_start=day.isoformat(),
                    coverage_end=day.isoformat(),
                    record_count=row_counts.get(day.isoformat(), 0),
                    status="partial" if failed and status == "ok" else status,
                    fetched_at_utc=scope.get("confirmed_at")
                    or scope.get("last_attempt_at")
                    or evaluated_at.isoformat(),
                    error="native acquisition unresolved" if failed else None,
                )
    return identity


def _descriptor(saved: InputSnapshot) -> dict[str, JsonValue]:
    saved = InputSnapshot.model_validate_json(saved.model_dump_json())
    if saved.kind != "native_l1" or saved.descriptor is None:
        raise ValueError("a digest-verified native price input is required")
    return saved.descriptor


def saved_days(saved: InputSnapshot) -> tuple[date, ...]:
    summary = _descriptor(saved)["summary"]
    if not isinstance(summary, dict):
        raise ValueError("native price input requires a selection summary")
    start, end = summary.get("selection_start"), summary.get("selection_end")
    if not isinstance(start, str) or not isinstance(end, str):
        raise ValueError("native price input requires its selected interval")
    return _days(date.fromisoformat(start), date.fromisoformat(end))


def materialize_saved_input(
    store: ObjectStore,
    saved: InputSnapshot,
    connection: duckdb.DuckDBPyConnection,
    *,
    days: Sequence[date],
) -> dict[str, Any]:
    saved_days(saved)
    counts: dict[str, Any] = {
        "days": len(days),
        "rows": 0,
        "unavailable": 0,
        "partial": 0,
        "incomplete": 0,
        "failed": 0,
        "held": 0,
    }
    complete = True
    for index, month in enumerate(_months(days)):
        selected = read_price_input(store, _descriptor(saved), days=month)
        scopes = selected["scopes"]
        requests = read_price_requests(
            store, scopes=scopes, receipts=selected["receipts"], days=month
        )
        results = rebuild_price_days(requests, days=month)
        materialize_price_days(
            connection, results.values(), l1_coverage_status="partial", append=index > 0
        )
        for day, item in results.items():
            scope = scopes[day.isoformat()]
            complete = complete and not _unresolved(item, scope)
            counts["rows"] += len(item.rows)
            counts["unavailable"] += int(item.status == "unavailable")
            counts["partial"] += int(item.status == "partial")
            counts["incomplete"] += int(bool(item.incomplete_requests))
            counts["failed"] += int(bool(scope.get("failure")))
            counts["held"] += int(bool(scope.get("held")))
    counts["coverage_status"] = "complete" if complete else "partial"
    connection.execute(
        "UPDATE price_receipt_status SET l1_coverage_status=?", [counts["coverage_status"]]
    )
    return counts


def rebuild_saved_input(
    store: ObjectStore, saved: InputSnapshot, *, start: date, end: date, output: Path
) -> dict[str, Any]:
    """Cold rebuild from immutable selection metadata and packs, without latest or L2."""
    days = _days(start, end)
    saved_days(saved)
    output.mkdir(parents=True, exist_ok=False)
    with duckdb.connect(":memory:") as connection:
        result = materialize_saved_input(store, saved, connection, days=days)
        for table in ("daily_bars", "price_receipt_status"):
            pq.write_table(connection.table(table).to_arrow_table(), output / (table + ".parquet"))
    selected = read_price_input(store, _descriptor(saved), days=days)
    write_json_atomic(
        output / "input.json",
        {
            "saved_input": saved.model_dump(mode="json"),
            "source_head_sha256": _descriptor(saved)["head_sha256"],
            "start": start.isoformat(),
            "end": end.isoformat(),
            **selected,
        },
    )
    return result


def rebuild(
    store: ObjectStore, fixed: FixedPriceL1, *, start: date, end: date, output: Path
) -> dict[str, Any]:
    """Rebuild an explicit interval directly from exact head bytes, without an execution DB."""
    days = _days(start, end)
    scopes = fixed.value()["scopes"]
    output.mkdir(parents=True, exist_ok=False)
    counts: dict[str, Any] = {
        "days": len(days),
        "rows": 0,
        "unavailable": 0,
        "partial": 0,
        "incomplete": 0,
    }
    complete = True
    with duckdb.connect(":memory:") as connection:
        for index, month in enumerate(_months(days)):
            results = rebuild_price_days(fixed.requests(store, days=month), days=month)
            materialize_price_days(
                connection, results.values(), l1_coverage_status="partial", append=index > 0
            )
            for day, item in results.items():
                complete = complete and not _unresolved(item, scopes.get(day.isoformat(), {}))
                counts["rows"] += len(item.rows)
                counts["unavailable"] += int(item.status == "unavailable")
                counts["partial"] += int(item.status == "partial")
                counts["incomplete"] += int(bool(item.incomplete_requests))
        counts["coverage_status"] = "complete" if complete else "partial"
        connection.execute(
            "UPDATE price_receipt_status SET l1_coverage_status=?", [counts["coverage_status"]]
        )
        for table in ("daily_bars", "price_receipt_status"):
            pq.write_table(connection.table(table).to_arrow_table(), output / (table + ".parquet"))
    (output / "head.json").write_bytes(fixed.payload)
    write_json_atomic(
        output / "input.json",
        {
            "head_sha256": fixed.content_id,
            "start": start.isoformat(),
            "end": end.isoformat(),
            "acquisition": {day.isoformat(): scopes.get(day.isoformat(), {}) for day in days},
        },
    )
    return counts


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--saved-input", type=Path)
    parser.add_argument("--work-directory", type=Path, required=True)
    parser.add_argument("--start", type=date.fromisoformat, required=True)
    parser.add_argument("--end", type=date.fromisoformat, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        with closing(
            Boto3R2Store(bucket=os.environ.get("R2_STORES_BUCKET") or "baibai-stores")
        ) as store:
            if args.saved_input is not None:
                saved = InputSnapshot.model_validate_json(args.saved_input.read_bytes())
                result = rebuild_saved_input(
                    store, saved, start=args.start, end=args.end, output=args.output
                )
            else:
                fixed = PriceL1(store, args.work_directory).fixed()
                result = rebuild(store, fixed, start=args.start, end=args.end, output=args.output)
            print(json.dumps(result))
    except Exception as error:
        print(f"price native rebuild failed: {type(error).__name__}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
