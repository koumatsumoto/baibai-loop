"""Reconstruct prices from one fixed native selection, never a previous L2."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import sys
from collections import Counter, defaultdict
from collections.abc import Iterable, Iterator, Sequence
from contextlib import closing, contextmanager
from datetime import date, datetime, timedelta
from itertools import batched, groupby
from operator import itemgetter
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

import pyarrow.parquet as pq  # type: ignore[import-untyped]
from pydantic import JsonValue

from baibai_engine.data_collection.providers.jquants.full_day_native import full_day_request_packs
from baibai_engine.data_collection.providers.jquants.price_native import (
    FixedPriceL1,
    PriceL1,
    prefetch_price_packs,
    read_price_requests,
)
from baibai_engine.machine_analysis.generation.contracts import InputSnapshot
from baibai_engine.machine_analysis.generation.price_inputs import (
    read_closed_price_days,
    read_price_input,
    retain_price_input,
    with_calendar_closures,
)
from baibai_engine.machine_analysis.observations.prices.from_l1 import (
    PRICE_BAR_SCHEMA,
    PriceBatchRows,
    PriceDay,
    PriceReceiptRow,
    price_bars_to_arrow,
    price_receipts_to_arrow,
    rebuild_price_days,
)
from baibai_engine.primitives.json import canonical_json
from baibai_engine.storage.json import write_json_atomic
from baibai_engine.storage.objects.r2 import Boto3R2Store, ObjectStore
from baibai_engine.storage.snapshots.models import CoverageStatus
from baibai_engine.storage.sqlite.source_cache.coverage import record_source_coverage

_PRICE_PACK_PREFETCH = 3


def _days(start: date, end: date) -> tuple[date, ...]:
    if start > end:
        raise ValueError("invalid price rebuild interval")
    return tuple(start + timedelta(days=i) for i in range((end - start).days + 1))


def _months(days: Sequence[date]) -> Iterator[tuple[date, ...]]:
    months: dict[str, list[date]] = defaultdict(list)
    for day in sorted(days):
        months[day.strftime("%Y-%m")].append(day)
    yield from (tuple(group) for group in months.values())


def _native_batches(
    days: Sequence[date], *, scopes: dict[str, Any], receipts: dict[str, Any]
) -> list[tuple[date, ...]]:
    """Avoid extra reads of any pack or extra batches; keep the existing 31-day bound."""
    monthly = list(_months(days))
    by_day = {
        day: frozenset(
            pack for _, pack in full_day_request_packs(scopes.get(day.isoformat(), {}), receipts)
        )
        for day in days
    }
    candidate: list[tuple[date, ...]] = []
    pending: tuple[date, ...] = ()
    for _, run in groupby(days, key=by_day.__getitem__):
        for part in batched(run, 31, strict=False):
            if len(pending) + len(part) > 31:
                candidate.append(pending)
                pending = ()
            pending += part
    if pending:
        candidate.append(pending)

    def reads(batches: Sequence[tuple[date, ...]]) -> Counter[str]:
        return Counter(
            pack for batch in batches for pack in set().union(*(by_day[d] for d in batch))
        )

    # A split correction pack can weigh much more than several other packs.
    # Accept only a strict per-pack improvement, not just a lower total count.
    return (
        candidate
        if len(candidate) <= len(monthly) and reads(candidate) < reads(monthly)
        else monthly
    )


def price_day_unresolved(item: PriceDay, scope: dict[str, Any]) -> bool:
    return bool(
        item.status in {"unavailable", "partial"}
        or item.incomplete_requests
        or scope.get("failure")
        or scope.get("held")
    )


def _retained_sql_text(raw: bytes) -> str | bytes:
    # A malformed TEXT value in this disposable projection cannot match native text.
    try:
        return raw.decode()
    except UnicodeDecodeError:
        return raw


def _same_price_day(
    connection: sqlite3.Connection, day: str, rebuilt: tuple[tuple[Any, ...], ...]
) -> bool:
    retained = connection.execute(
        "SELECT * FROM jquants_daily_bars WHERE traded_at=? ORDER BY ticker", (day,)
    ).fetchall()
    # Bound this comparison to one day. Missing, extra or changed values require a rewrite.
    return len(retained) == len(rebuilt) and retained == sorted(rebuilt, key=itemgetter(0))


def model_input(
    store: ObjectStore,
    fixed: FixedPriceL1,
    *,
    as_of: date,
    evaluated_at: datetime,
    market: Path,
    calendar_input: InputSnapshot | None = None,
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
    scopes = with_calendar_closures(store, scopes, calendar_input, evaluated_at=evaluated_at)
    closed = {
        date.fromisoformat(day) for day, scope in scopes.items() if "closed_calendar" in scope
    }
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
        "closed_unrequested_days": len(closed),
    }
    ranges: list[dict[str, str]] = []
    required_unresolved = False
    uri = market.resolve().as_uri() + "?mode=rw"
    batches = _native_batches(days, scopes=head["scopes"], receipts=head["receipts"])
    pack_reads = prefetch_price_packs(
        store,
        scopes=head["scopes"],
        receipts=head["receipts"],
        batches=batches,
        width=_PRICE_PACK_PREFETCH,
    )
    with closing(sqlite3.connect(uri, uri=True)) as connection, connection, pack_reads as read_pack:
        connection.text_factory = _retained_sql_text
        # The disposable execution store is the staging area. Readers see the old
        # projection until every native page and the fixed selection have succeeded.
        connection.execute("BEGIN IMMEDIATE")
        prefilled = connection.execute("SELECT 1 FROM jquants_daily_bars LIMIT 1").fetchone()
        if prefilled:
            # Remove obsolete dates inside SQLite, even ill-formed TEXT in an old store.
            connection.execute("CREATE TEMP TABLE native_price_days(day TEXT PRIMARY KEY)")
            connection.executemany(
                "INSERT INTO native_price_days VALUES(?)", ((day.isoformat(),) for day in days)
            )
            connection.execute(
                "DELETE FROM jquants_daily_bars WHERE traded_at IN ("
                "SELECT traded_at FROM jquants_daily_bars GROUP BY traded_at "
                "EXCEPT SELECT day FROM native_price_days)"
            )
        connection.execute("DELETE FROM jquants_source_pages WHERE source='daily_bars'")
        connection.execute("DELETE FROM source_coverage WHERE source='jquants_daily_bars'")
        for batch in batches:
            prepared = PriceBatchRows()
            requests = tuple(
                read_price_requests(
                    store,
                    scopes=head["scopes"],
                    receipts=head["receipts"],
                    days=batch,
                    consume=prepared.consume,
                    read_pack=read_pack,
                )
            )
            if any(
                item.last_received_at is not None and item.last_received_at > evaluated_at
                for item in requests
            ):
                raise ValueError("price input contains receipts later than the evaluation clock")
            results = rebuild_price_days(requests, days=batch, prepared=prepared)
            for day, item in results.items():
                key = day.isoformat()
                if not prefilled or not _same_price_day(connection, key, item.rows):
                    if prefilled:
                        connection.execute(
                            "DELETE FROM jquants_daily_bars WHERE traded_at=?", (key,)
                        )
                    connection.executemany(
                        "INSERT INTO jquants_daily_bars VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        item.rows,
                    )
                scope = scopes[key]
                failed = bool(scope.get("failure") or scope.get("held") or item.incomplete_requests)
                record_source_coverage(
                    connection,
                    source="jquants_daily_bars",
                    coverage_key="native:" + key,
                    coverage_start=key,
                    coverage_end=key,
                    record_count=len(item.rows),
                    status="partial" if failed and item.status == "ok" else item.status,
                    fetched_at_utc=scope.get("confirmed_at")
                    or scope.get("last_attempt_at")
                    or evaluated_at.isoformat(),
                    error="native acquisition unresolved" if failed else None,
                )
                unresolved = price_day_unresolved(item, scope)
                required_unresolved = required_unresolved or (unresolved and day not in closed)
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
        summary["raw_coverage_status"] = "partial" if ranges else "complete"
        summary["coverage_status"] = "partial" if required_unresolved else "complete"
        selected["summary"] = summary
        descriptor = retain_price_input(store, selected)
        digest = hashlib.sha256(canonical_json(descriptor).encode()).hexdigest()
        identity = InputSnapshot(
            kind="native_l1",
            reference="jquants-price-" + digest,
            sha256=digest,
            descriptor=descriptor,
        )
    return identity


def _descriptor(saved: InputSnapshot) -> dict[str, JsonValue]:
    saved = InputSnapshot.model_validate_json(saved.model_dump_json())
    if saved.kind != "native_l1" or saved.descriptor is None:
        raise ValueError("a digest-verified native price input is required")
    return saved.descriptor


def saved_interval(saved: InputSnapshot) -> tuple[date, date]:
    return _saved_interval(_descriptor(saved))


def _saved_interval(descriptor: dict[str, JsonValue]) -> tuple[date, date]:
    summary = descriptor["summary"]
    if not isinstance(summary, dict):
        raise ValueError("native price input requires a selection summary")
    start, end = summary.get("selection_start"), summary.get("selection_end")
    if not isinstance(start, str) or not isinstance(end, str):
        raise ValueError("native price input requires its selected interval")
    first, last = date.fromisoformat(start), date.fromisoformat(end)
    if first > last:
        raise ValueError("invalid saved price interval")
    return first, last


@contextmanager
def _cold_price_output(output: Path, *, include_head: bool = False) -> Iterator[Path]:
    if output.exists(follow_symlinks=False):
        raise FileExistsError(f"price rebuild output already exists: {output}")
    output.parent.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(prefix=".price-rebuild-", dir=output.parent) as directory:
        staged = Path(directory)
        yield staged
        # mkdir must not replace even an empty directory claimed by another rebuild.
        output.mkdir()
        names = ("daily_bars.parquet", "price_receipt_status.parquet")
        for name in (*names, "head.json") if include_head else names:
            (staged / name).rename(output / name)
        (staged / "input.json").rename(output / "input.json")


def _write_cold_price_parquet(
    store: ObjectStore,
    *,
    scopes: dict[str, Any],
    receipts: dict[str, Any],
    batches: Iterable[Sequence[date]],
    staged: Path,
    saved_selection: bool,
) -> dict[str, Any]:
    counts: dict[str, Any] = {
        "days": 0,
        "rows": 0,
        "unavailable": 0,
        "partial": 0,
        "incomplete": 0,
    }
    if saved_selection:
        counts.update(failed=0, held=0, closed_unrequested=0)
    complete = raw_complete = True
    status_rows: list[PriceReceiptRow] = []
    with pq.ParquetWriter(staged / "daily_bars.parquet", PRICE_BAR_SCHEMA) as writer:
        for batch in batches:
            closed = (
                read_closed_price_days(
                    store, {day.isoformat(): scopes[day.isoformat()] for day in batch}
                )
                if saved_selection
                else set()
            )
            requests = read_price_requests(store, scopes=scopes, receipts=receipts, days=batch)
            results = rebuild_price_days(requests, days=batch)
            bars = price_bars_to_arrow(results.values())
            if bars.num_rows:
                writer.write_table(bars)
            counts["rows"] += bars.num_rows
            counts["days"] += len(batch)
            for day, item in results.items():
                scope = scopes.get(day.isoformat(), {})
                unresolved = price_day_unresolved(item, scope)
                raw_complete = raw_complete and not unresolved
                complete = complete and (not unresolved or day in closed)
                counts["unavailable"] += int(item.status == "unavailable")
                counts["partial"] += int(item.status == "partial")
                counts["incomplete"] += int(bool(item.incomplete_requests))
                if saved_selection:
                    counts["failed"] += int(bool(scope.get("failure")))
                    counts["held"] += int(bool(scope.get("held")))
                    counts["closed_unrequested"] += int(day in closed)
                status_rows.append(item.receipt_row())
    counts["coverage_status"] = "complete" if complete else "partial"
    raw_coverage: CoverageStatus = "complete" if raw_complete else "partial"
    if saved_selection:
        counts["raw_coverage_status"] = raw_coverage
    pq.write_table(
        price_receipts_to_arrow(status_rows, l1_coverage_status=raw_coverage),
        staged / "price_receipt_status.parquet",
    )
    return counts


def rebuild_saved_input(
    store: ObjectStore, saved: InputSnapshot, *, start: date, end: date, output: Path
) -> dict[str, Any]:
    """Cold rebuild from immutable selection metadata and packs, without latest or L2."""
    days = _days(start, end)
    descriptor = _descriptor(saved)
    _saved_interval(descriptor)
    with _cold_price_output(output) as staged:
        selected = read_price_input(store, descriptor, days=days)
        result = _write_cold_price_parquet(
            store,
            scopes=selected["scopes"],
            receipts=selected["receipts"],
            batches=_months(days),
            staged=staged,
            saved_selection=True,
        )
        write_json_atomic(
            staged / "input.json",
            {
                "saved_input": saved.model_dump(mode="json"),
                "source_head_sha256": descriptor["head_sha256"],
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
    head = fixed.value()
    scopes = head["scopes"]
    receipts = head["receipts"]
    with _cold_price_output(output, include_head=True) as staged:
        result = _write_cold_price_parquet(
            store,
            scopes=scopes,
            receipts=receipts,
            batches=_native_batches(days, scopes=scopes, receipts=receipts),
            staged=staged,
            saved_selection=False,
        )
        (staged / "head.json").write_bytes(fixed.payload)
        write_json_atomic(
            staged / "input.json",
            {
                "head_sha256": fixed.content_id,
                "start": start.isoformat(),
                "end": end.isoformat(),
                "acquisition": {day.isoformat(): scopes.get(day.isoformat(), {}) for day in days},
            },
        )
    return result


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--saved-input", type=Path)
    parser.add_argument("--work-directory", type=Path, required=True)
    parser.add_argument("--start", type=date.fromisoformat, required=True)
    parser.add_argument("--end", type=date.fromisoformat, required=True)
    parser.add_argument("--output", type=Path, required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
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
