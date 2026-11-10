"""Acquire full calendar ranges and commit day fulfillment without a market release."""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import uuid
from collections.abc import Callable, Iterable
from contextlib import closing
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

from baibai_engine.data_collection.providers.jquants.calendar_native import (
    CalendarAttempt,
    CalendarL1,
    FixedCalendarL1,
)
from baibai_engine.data_collection.providers.jquants.contracts import (
    JQuantsPage,
    JQuantsRateLimitError,
)
from baibai_engine.data_collection.providers.jquants.http import JQuantsHTTP
from baibai_engine.data_collection.providers.jquants.market_rules import (
    CALENDAR_FORWARD_DAYS,
    CALENDAR_LOOKBACK_DAYS,
)
from baibai_engine.data_collection.transport.http import AcquisitionDeadlineExceeded
from baibai_engine.storage.json import write_json_atomic
from baibai_engine.storage.objects.r2 import Boto3R2Store


def calendar_window(fixed: FixedCalendarL1, asof: date) -> tuple[date, date]:
    first = asof - timedelta(days=CALENDAR_LOOKBACK_DAYS)
    return min(
        [first, *(date.fromisoformat(day) for day in fixed.value()["days"])]
    ), asof + timedelta(days=CALENDAR_FORWARD_DAYS)


def calendar_ranges(days: Iterable[date]) -> tuple[tuple[date, date], ...]:
    result: list[tuple[date, date]] = []
    for day in sorted(set(days)):
        if result and result[-1][1] + timedelta(days=1) == day:
            result[-1] = result[-1][0], day
        else:
            result.append((day, day))
    return tuple(result)


def acquire_calendar_native(
    owner: CalendarL1,
    *,
    start: date,
    end: date,
    client: JQuantsHTTP,
    force: bool = False,
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    sleep: Callable[[float], None] = time.sleep,
) -> dict[str, int]:
    fixed = owner.fixed()
    if start > end:
        raise ValueError("calendar acquisition range is reversed")
    if any(owner.directory.glob("attempt-*.json")):
        raise ValueError("use a fresh directory or commit-work for saved calendar attempts")
    days = fixed.plan(start=start, end=end)
    ranges = ((start, end),) if force else calendar_ranges(days)
    totals = dict.fromkeys(("changed", "equal", "failed", "held", "retried", "halted"), 0)
    for index, (first, last) in enumerate(ranges):
        if index and index % 31 == 0:
            sleep(3.0)
        identity, at = uuid.uuid4().hex, clock()
        pages: list[JQuantsPage] = []
        path = owner.directory / ("attempt-" + identity + ".json")
        write_json_atomic(path, CalendarAttempt(identity, first, last, at, ()).value())
        failure = None
        try:
            for page in client.pages(
                "market_calendar", {"from": first.strftime("%Y%m%d"), "to": last.strftime("%Y%m%d")}
            ):
                pages.append(page)
                write_json_atomic(
                    path, CalendarAttempt(identity, first, last, at, tuple(pages)).value()
                )
        except Exception as error:
            failure = type(error).__name__
            totals["halted"] = int(
                isinstance(error, (JQuantsRateLimitError, AcquisitionDeadlineExceeded))
            )
        item = CalendarAttempt(identity, first, last, at, tuple(pages), failure)
        write_json_atomic(path, item.value())
        for key, ids in owner.commit([item]).items():
            totals[key] += len(ids)
        if failure:
            break
    totals["incomplete"] = len(owner.fixed().plan(start=start, end=end))
    return totals


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "command", choices=("initialize", "resume", "plan", "acquire", "commit-work")
    )
    parser.add_argument("--directory", required=True, type=Path)
    parser.add_argument("--start", type=date.fromisoformat)
    parser.add_argument("--end", type=date.fromisoformat)
    parser.add_argument("--force", action="store_true", help="recheck this explicit full range")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        with closing(
            Boto3R2Store(
                bucket=os.environ.get("R2_STORES_BUCKET") or "baibai-stores",
                verify_put_with_head=False,
            )
        ) as store:
            owner = CalendarL1(store, args.directory)
            if args.command == "initialize":
                print(owner.initialize().content_id)
            elif args.command == "resume":
                print(owner.fixed().content_id)
            elif args.command == "commit-work":
                attempts = [
                    CalendarAttempt.from_value(json.loads(path.read_bytes()))
                    for path in args.directory.glob("attempt-*.json")
                ]
                attempts.sort(key=lambda item: (item.order_time, item.request_id))
                replayed = owner.commit(attempts)
                fixed = owner.fixed()
                unresolved = sorted(
                    {
                        day.isoformat()
                        for item in attempts
                        for day in fixed.plan(start=item.start, end=item.end)
                    }
                )
                print(json.dumps({**replayed, "unresolved": unresolved}, sort_keys=True))
                return int(bool(unresolved))
            else:
                if args.start is None or args.end is None:
                    raise ValueError("--start and --end are required")
                if args.command == "plan":
                    print(
                        json.dumps(
                            [
                                day.isoformat()
                                for day in owner.fixed().plan(start=args.start, end=args.end)
                            ]
                        )
                    )
                else:
                    with closing(JQuantsHTTP(os.environ.get("JQUANTS_API_KEY", ""))) as client:
                        report = acquire_calendar_native(
                            owner, start=args.start, end=args.end, client=client, force=args.force
                        )
                    print(json.dumps(report, sort_keys=True))
                    return int(bool(report["failed"] or report["held"] or report["incomplete"]))
        return 0
    except Exception as error:
        print(f"calendar native L1 failed: {type(error).__name__}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
