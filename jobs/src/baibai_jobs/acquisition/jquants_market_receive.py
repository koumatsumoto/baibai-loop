"""Receive market originals from L1-derived plans without previous L2 inputs."""

from __future__ import annotations

import sqlite3
import time
from collections.abc import Callable, Iterable
from contextlib import closing
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

from baibai_engine.data_collection.providers.jquants.acquisition import acquire_request
from baibai_engine.data_collection.providers.jquants.contract import JQUANTS_SOURCE_PAGES_DDL
from baibai_engine.data_collection.providers.jquants.contracts import (
    JQuantsAcquisitionError,
    JQuantsRateLimitError,
    JQuantsSource,
)
from baibai_engine.data_collection.providers.jquants.dependencies import export_dependencies
from baibai_engine.data_collection.providers.jquants.http import JQuantsHTTP
from baibai_engine.data_collection.providers.jquants.native_index import (
    NativeMarketIndex,
    calendar_sessions,
    index_market_originals,
)
from baibai_engine.data_collection.transport.http import AcquisitionDeadlineExceeded
from baibai_engine.storage.json import write_json_atomic
from baibai_jobs.acquisition.jquants_market_plan import (
    FixedMarketIndexes,
    MarketPlans,
    plan_market_from_originals,
)
from baibai_jobs.acquisition.jquants_pending import iter_pending_requests


def _ranges(days: Iterable[date]) -> tuple[tuple[date, date], ...]:
    ranges: list[tuple[date, date]] = []
    for day in sorted(set(days)):
        if ranges and day == ranges[-1][1] + timedelta(days=1):
            ranges[-1] = ranges[-1][0], day
        else:
            ranges.append((day, day))
    return tuple(ranges)


def receive_market_handoff(
    directory: Path,
    fixed: FixedMarketIndexes,
    *,
    asof: date,
    client: JQuantsHTTP,
    scheduled: bool = False,
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    sleep: Callable[[float], None] = time.sleep,
) -> bool:
    """Stage originals even if one source fails; use new calendar receipts in the same run."""
    directory.mkdir(parents=True, exist_ok=True)
    output = directory / "receipts.sqlite"
    if output.exists():
        raise FileExistsError(output)
    indexes = dict(fixed.indexes)
    errors: set[str] = set()
    seen: set[str] = set()
    halted = False
    skipped = False
    with closing(sqlite3.connect(output)) as conn:
        conn.executescript(JQUANTS_SOURCE_PAGES_DDL)

        def receive(source: JQuantsSource, queries: Iterable[dict[str, str]]) -> None:
            nonlocal halted
            if halted:
                return
            for count, query in enumerate(queries):
                if count and count % 31 == 0:
                    sleep(3.0)
                try:
                    acquire_request(conn, client, source, query)
                except (JQuantsRateLimitError, AcquisitionDeadlineExceeded):
                    errors.add(source)
                    halted = True
                    break
                except JQuantsAcquisitionError:
                    errors.add(source)
                    break
            # Index the pages actually received, including a partial final request.
            fresh = index_market_originals(
                iter_pending_requests(output, source=source, known_at=clock()), source=source
            )
            existing = indexes[source]
            new_receipts = tuple(item for item in fresh.receipts if item.request_id not in seen)
            new_incomplete = tuple(item for item in fresh.incomplete if item.request_id not in seen)
            existing_ids = {item.request_id for item in existing.receipts} | {
                item.request_id for item in existing.incomplete
            }
            new_ids = {item.request_id for item in new_receipts} | {
                item.request_id for item in new_incomplete
            }
            if existing_ids & new_ids:
                raise ValueError("new market request identity already exists")
            seen.update(new_ids)
            indexes[source] = NativeMarketIndex(
                source, existing.receipts + new_receipts, existing.incomplete + new_incomplete
            )

        def plan() -> MarketPlans:
            return plan_market_from_originals(indexes, asof=asof)

        unknown: set[str] = set(fixed.unavailable_stages)
        try:
            sessions, calendar_unknown = calendar_sessions(
                indexes["market_calendar"], start=asof, end=asof
            )
            skipped = scheduled and sessions[asof] is False and not calendar_unknown
            if not skipped:
                master = plan().master
                receive("master", ({"date": day.isoformat()} for day in master.days))
                prices = plan().daily_prices
                receive("daily_bars", ({"date": day.isoformat()} for day in prices.days))
                calendar = plan().calendar
                receive(
                    "market_calendar",
                    (
                        {"from": first.strftime("%Y%m%d"), "to": last.strftime("%Y%m%d")}
                        for first, last in _ranges(calendar.days)
                    ),
                )
                weekly = plan().weekly_margin
                receive(
                    "margin_interest", ({"date": day.strftime("%Y%m%d")} for day in weekly.days)
                )
                alerts = plan().margin_alerts
                receive("margin_alerts", ({"date": day.isoformat()} for day in alerts.days))
                daily = plan().daily_margin
                receive("margin_interest", ({"date": day.strftime("%Y%m%d")} for day in daily.days))
                shorts = plan().short_sales
                receive(
                    "short_sale_reports", ({"disc_date": day.isoformat()} for day in shorts.days)
                )
                final = plan()
                unknown = set(fixed.unavailable_stages)
                for value in (
                    final.master,
                    final.daily_prices,
                    final.calendar,
                    final.weekly_margin,
                    final.daily_margin,
                    final.margin_alerts,
                    final.short_sales,
                ):
                    unknown.update(value.unknown_incomplete_requests)
                if (
                    final.weekly_margin.unknown_calendar_days
                    or final.daily_margin.unknown_calendar_days
                ):
                    unknown.add("market-calendar-days")
                if final.master.days:
                    errors.add("master_incomplete")
                if final.calendar.days:
                    errors.add("calendar_incomplete")
                if final.daily_margin.days:
                    errors.add("daily_margin_incomplete")
        except Exception as error:
            # Preserve pages already received; surface failure without leaking payloads.
            errors.add(type(error).__name__)
        ids = [
            row[0]
            for row in conn.execute(
                "SELECT DISTINCT request_id FROM jquants_source_pages ORDER BY request_id"
            )
        ]
    if unknown:
        errors.add("unavailable_originals")
    dependencies = {item["source"]: item for item in fixed.dependencies}
    try:
        for item in export_dependencies(output, day=asof, master_asof=asof):
            previous = dependencies.get(item["source"])
            if previous is None or (item["received_at"], item["request_id"]) > (
                previous["received_at"],
                previous["request_id"],
            ):
                dependencies[item["source"]] = item
        for item in dependencies.values():
            write_json_atomic(directory / f"dependency-{item['request_id']}.json", item)
    except Exception as error:
        errors.add(type(error).__name__)
    write_json_atomic(
        directory / "result.json",
        {
            "source": "jquants-market",
            "asof": asof.isoformat(),
            "started_at": fixed.known_at.isoformat(),
            "request_ids": ids,
            "planning": {
                "release_id": fixed.release_id,
                "manifest_sha256": fixed.manifest_sha256,
                "l1_coverage_status": fixed.l1_coverage_status,
            },
            "acquisition": {
                "status": "failed"
                if errors
                else "skipped_non_business_day"
                if skipped
                else "received",
                "failed_sources": sorted(errors),
            },
        },
    )
    return not errors
