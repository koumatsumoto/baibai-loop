"""Acquire explicit master snapshots independently of L2 and market releases."""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from collections.abc import Callable
from contextlib import closing
from datetime import UTC, date, datetime, timedelta

from baibai_engine.data_collection.providers.jquants.http import JQuantsHTTP
from baibai_engine.data_collection.providers.jquants.master_native import (
    MasterAttempt,
    MasterL1,
    plan_master_days,
)
from baibai_engine.storage.objects.r2 import Boto3R2Store
from baibai_jobs.acquisition.jquants_full_day_native import (
    acquire_full_day_native,
    native_main,
)
from baibai_jobs.acquisition.jquants_full_day_native import (
    build_parser as build_full_day_parser,
)


def acquire_master_native(
    owner: MasterL1,
    *,
    start: date,
    end: date,
    client: JQuantsHTTP,
    force: bool = False,
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    sleep: Callable[[float], None] = time.sleep,
) -> dict[str, int]:
    """Keep full raw empty receipts, while leaving their requested dates unfulfilled."""
    days = plan_master_days(owner.store, owner.resume(), start=start, end=end)
    if force:
        days = tuple(start + timedelta(days=offset) for offset in range((end - start).days + 1))
    result = acquire_full_day_native(
        owner,
        start=start,
        end=end,
        revision_overlap_days=0,
        planned_days=days,
        client=client,
        clock=clock,
        sleep=sleep,
    )
    result["incomplete"] = len(plan_master_days(owner.store, owner.fixed(), start=start, end=end))
    return result


def build_parser() -> argparse.ArgumentParser:
    parser = build_full_day_parser(__doc__ or "")
    parser.set_defaults(revision_overlap_days=0)
    parser.add_argument("--force", action="store_true", help="recheck explicitly requested dates")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command not in {"plan", "acquire", "commit-work"}:
        return native_main(
            argv,
            parser=build_parser(),
            owner_type=MasterL1,
            attempt_type=MasterAttempt,
            store_factory=Boto3R2Store,
            client_factory=JQuantsHTTP,
        )
    try:
        if args.command != "commit-work" and (args.start is None or args.end is None):
            raise ValueError("--start and --end are required")
        if args.revision_overlap_days:
            raise ValueError(
                "master snapshots have no automatic revision tail; request explicit dates"
            )
        with closing(
            Boto3R2Store(bucket=os.environ.get("R2_STORES_BUCKET") or "baibai-stores")
        ) as store:
            owner = MasterL1(store, args.directory)
            if args.command == "commit-work":
                attempts = [
                    MasterAttempt.from_value(json.loads(path.read_bytes()))
                    for path in args.directory.glob("attempt-*.json")
                ]
                attempts.sort(
                    key=lambda item: (item.attempted_at or item.order_time, item.request_id)
                )
                replayed = owner.commit(attempts)
                fixed = owner.fixed()
                unresolved = {
                    day.isoformat(): ["nonempty_master_required"]
                    for day in sorted({item.day for item in attempts})
                    if plan_master_days(store, fixed, start=day, end=day)
                }
                print(json.dumps({**replayed, "unresolved": unresolved}, sort_keys=True))
                return int(bool(unresolved))
            if args.command == "plan":
                print(
                    json.dumps(
                        [
                            day.isoformat()
                            for day in plan_master_days(
                                store, owner.fixed(), start=args.start, end=args.end
                            )
                        ]
                    )
                )
                return 0
            with closing(JQuantsHTTP(os.environ.get("JQUANTS_API_KEY", ""))) as client:
                report = acquire_master_native(
                    owner, start=args.start, end=args.end, client=client, force=args.force
                )
            print(json.dumps(report, sort_keys=True))
            return int(bool(report["failed"] or report["held"] or report["incomplete"]))
    except Exception as error:
        print(f"master native L1 failed: {type(error).__name__}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
