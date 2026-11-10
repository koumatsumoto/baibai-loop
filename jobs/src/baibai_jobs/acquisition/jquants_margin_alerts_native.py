"""信用取引日々公表銘柄をL2やmarket releaseに依存せず差分取得する。"""

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

from baibai_engine.data_collection.providers.jquants.http import JQuantsHTTP
from baibai_engine.data_collection.providers.jquants.margin_alerts_native import (
    MarginAlertsAttempt,
    MarginAlertsL1,
    plan_margin_alert_days,
)
from baibai_engine.storage.objects.r2 import Boto3R2Store
from baibai_jobs.acquisition.jquants_full_day_native import (
    acquire_full_day_native,
    native_main,
)


def acquire_margin_alerts_native(
    owner: MarginAlertsL1,
    *,
    asof: date,
    client: JQuantsHTTP,
    start: date | None = None,
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    sleep: Callable[[float], None] = time.sleep,
) -> dict[str, int]:
    days = plan_margin_alert_days(owner.resume(), asof=asof, start=start)
    return acquire_full_day_native(
        owner,
        start=start or asof,
        end=asof,
        revision_overlap_days=0,
        planned_days=days,
        client=client,
        clock=clock,
        sleep=sleep,
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "command", choices=("initialize", "plan", "acquire", "resume", "commit-work")
    )
    parser.add_argument("--directory", type=Path, required=True)
    parser.add_argument("--start", type=date.fromisoformat)
    parser.add_argument("--asof", type=date.fromisoformat)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command not in {"plan", "acquire"}:
        return native_main(
            argv,
            parser=parser,
            owner_type=MarginAlertsL1,
            attempt_type=MarginAlertsAttempt,
            store_factory=Boto3R2Store,
            client_factory=JQuantsHTTP,
        )
    try:
        if args.asof is None:
            raise ValueError("margin alerts requires --asof")
        with closing(
            Boto3R2Store(
                bucket=os.environ.get("R2_STORES_BUCKET") or "baibai-stores",
                verify_put_with_head=False,
            )
        ) as store:
            owner = MarginAlertsL1(store, args.directory)
            if args.command == "plan":
                days = plan_margin_alert_days(owner.fixed(), asof=args.asof, start=args.start)
                print(json.dumps([day.isoformat() for day in days]))
                return 0
            with closing(JQuantsHTTP(os.environ.get("JQUANTS_API_KEY", ""))) as client:
                report = acquire_margin_alerts_native(
                    owner, asof=args.asof, start=args.start, client=client
                )
            print(json.dumps(report, sort_keys=True))
            return int(bool(report["failed"] or report["held"]))
    except Exception as error:
        print(f"margin alerts native L1 failed: {type(error).__name__}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
