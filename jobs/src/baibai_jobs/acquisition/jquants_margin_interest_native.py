"""固定native calendarから全銘柄信用残を取得し、L2と独立して原本を確定する。"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from collections.abc import Callable
from contextlib import closing
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

from baibai_engine.data_collection.providers.jquants.calendar_native import (
    CalendarL1,
    FixedCalendarL1,
)
from baibai_engine.data_collection.providers.jquants.http import JQuantsHTTP
from baibai_engine.data_collection.providers.jquants.margin_interest_native import (
    MarginInterestAttempt,
    MarginInterestL1,
    plan_margin_interest_days,
)
from baibai_engine.data_collection.providers.jquants.native_index import MarginAcquisitionPlan
from baibai_engine.storage.json import write_json_atomic
from baibai_engine.storage.objects.r2 import Boto3R2Store
from baibai_jobs.acquisition.jquants_full_day_native import acquire_full_day_native, native_main


def _plan_report(plan: MarginAcquisitionPlan) -> dict[str, Any]:
    return {
        "days": [day.isoformat() for day in plan.days],
        "unknown_calendar_days": [day.isoformat() for day in plan.unknown_calendar_days],
        "unknown_incomplete_requests": list(plan.unknown_incomplete_requests),
    }


def acquire_margin_interest_native(
    owner: MarginInterestL1,
    calendar: FixedCalendarL1,
    *,
    asof: date,
    client: JQuantsHTTP,
    start: date | None = None,
    end: date | None = None,
    force: bool = False,
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    sleep: Callable[[float], None] = time.sleep,
) -> dict[str, Any]:
    plan = plan_margin_interest_days(
        owner.resume(), calendar, asof=asof, start=start, end=end, force=force
    )
    if any(owner.directory.glob("attempt-*.json")):
        raise ValueError("use a fresh work directory or commit-work for saved attempts")
    write_json_atomic(
        owner.directory / "plan.json",
        {
            "asof": asof.isoformat(),
            "start": start.isoformat() if start else None,
            "end": end.isoformat() if end else None,
            "force": force,
            "calendar_sha256": calendar.content_id,
            **_plan_report(plan),
        },
    )
    (owner.directory / "calendar-fixed.json").write_bytes(calendar.payload)
    report: dict[str, Any] = dict(
        acquire_full_day_native(
            owner,
            start=start or asof,
            end=end or asof,
            revision_overlap_days=0,
            planned_days=plan.days,
            client=client,
            clock=clock,
            sleep=sleep,
        )
    )
    remaining = plan_margin_interest_days(owner.fixed(), calendar, asof=asof, start=start, end=end)
    report.update(
        {
            "calendar_sha256": calendar.content_id,
            "incomplete": len(remaining.days),
            "remaining": _plan_report(remaining),
        }
    )
    write_json_atomic(owner.directory / "result.json", report)
    return report


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "command", choices=("initialize", "plan", "acquire", "resume", "commit-work")
    )
    parser.add_argument("--directory", type=Path, required=True)
    parser.add_argument("--asof", type=date.fromisoformat)
    parser.add_argument("--start", type=date.fromisoformat)
    parser.add_argument("--end", type=date.fromisoformat)
    parser.add_argument(
        "--force", action="store_true", help="明示した有限区間の充足済み日も再取得する"
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command in {"initialize", "resume"}:
        return native_main(
            argv,
            parser=parser,
            owner_type=MarginInterestL1,
            attempt_type=MarginInterestAttempt,
            store_factory=Boto3R2Store,
            client_factory=JQuantsHTTP,
        )
    try:
        with closing(
            Boto3R2Store(bucket=os.environ.get("R2_STORES_BUCKET") or "baibai-stores")
        ) as store:
            owner = MarginInterestL1(store, args.directory)
            if args.command == "commit-work":
                # Reuse the acquisition's exact calendar and requested scope. Replaying
                # identities is not evidence that a daily empty or an unattempted day is fulfilled.
                saved_plan = json.loads((args.directory / "plan.json").read_bytes())
                calendar = FixedCalendarL1(
                    (args.directory / "calendar-fixed.json").read_bytes(), "saved"
                )
                if calendar.content_id != saved_plan["calendar_sha256"]:
                    raise ValueError("saved acquisition calendar changed")
                attempts = [
                    MarginInterestAttempt.from_value(json.loads(path.read_bytes()))
                    for path in args.directory.glob("attempt-*.json")
                ]
                attempts.sort(key=lambda item: (item.order_time, item.request_id))
                if any(item.day.isoformat() not in saved_plan["days"] for item in attempts):
                    raise ValueError("saved attempt is outside its acquisition plan")
                replayed = owner.commit(attempts)
                remaining = plan_margin_interest_days(
                    owner.fixed(),
                    calendar,
                    asof=date.fromisoformat(saved_plan["asof"]),
                    start=date.fromisoformat(saved_plan["start"]) if saved_plan["start"] else None,
                    end=date.fromisoformat(saved_plan["end"]) if saved_plan["end"] else None,
                )
                print(
                    json.dumps({**replayed, "remaining": _plan_report(remaining)}, sort_keys=True)
                )
                return int(
                    bool(
                        remaining.days
                        or remaining.unknown_calendar_days
                        or remaining.unknown_incomplete_requests
                    )
                )
            if args.asof is None:
                raise ValueError("margin interest requires --asof")
            calendar = CalendarL1(store, args.directory / "calendar").fixed()
            if args.command == "plan":
                plan = plan_margin_interest_days(
                    owner.fixed(),
                    calendar,
                    asof=args.asof,
                    start=args.start,
                    end=args.end,
                    force=args.force,
                )
                print(
                    json.dumps(
                        {"calendar_sha256": calendar.content_id, **_plan_report(plan)},
                        sort_keys=True,
                    )
                )
                return int(bool(plan.unknown_calendar_days or plan.unknown_incomplete_requests))
            with closing(JQuantsHTTP(os.environ.get("JQUANTS_API_KEY", ""))) as client:
                report = acquire_margin_interest_native(
                    owner,
                    calendar,
                    asof=args.asof,
                    start=args.start,
                    end=args.end,
                    force=args.force,
                    client=client,
                )
            print(json.dumps(report, sort_keys=True))
            return int(
                bool(
                    report["failed"]
                    or report["held"]
                    or report["incomplete"]
                    or report["remaining"]["unknown_calendar_days"]
                    or report["remaining"]["unknown_incomplete_requests"]
                )
            )
    except Exception as error:
        print(f"margin interest native L1 failed: {type(error).__name__}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
