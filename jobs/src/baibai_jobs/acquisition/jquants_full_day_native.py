"""Acquire explicit full-day originals and commit them without L2 or release state."""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import uuid
from collections.abc import Callable
from contextlib import closing
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

from baibai_engine.data_collection.providers.jquants.contracts import (
    JQuantsPage,
    JQuantsRateLimitError,
)
from baibai_engine.data_collection.providers.jquants.full_day_native import (
    FullDayAttempt,
    FullDayL1,
)
from baibai_engine.data_collection.providers.jquants.http import JQuantsHTTP
from baibai_engine.data_collection.transport.http import AcquisitionDeadlineExceeded
from baibai_engine.storage.json import write_json_atomic


def acquire_full_day_native(
    owner: FullDayL1[Any, Any],
    *,
    start: date,
    end: date,
    revision_overlap_days: int,
    client: JQuantsHTTP,
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    sleep: Callable[[float], None] = time.sleep,
) -> dict[str, int]:
    """Commit bounded groups; preserve a partial response and stop on provider failure."""
    fixed = owner.resume()
    days = fixed.plan(start=start, end=end, revision_overlap_days=revision_overlap_days)
    if any(owner.directory.glob("attempt-*.json")):
        raise ValueError("use a fresh work directory or commit-work for saved attempts")
    totals = dict.fromkeys(("changed", "equal", "failed", "held", "retried"), 0)
    totals["halted"] = 0
    attempts: list[FullDayAttempt] = []

    def commit() -> None:
        report = owner.commit(attempts)
        for key, values in report.items():
            totals[key] += len(values)
        attempts.clear()

    for index, day in enumerate(days):
        if index and index % 31 == 0:
            commit()
            sleep(3.0)
        identity, started = uuid.uuid4().hex, clock()
        pages: list[JQuantsPage] = []
        failure = None
        path = owner.directory / ("attempt-" + identity + ".json")
        # Write identity before acquisition; each page is retained before the
        # generator is resumed to request the following page.
        write_json_atomic(path, owner.attempt_type(identity, day, started, ()).value())
        try:
            for page in client.pages(owner.contract.source, {"date": day.strftime("%Y%m%d")}):
                pages.append(page)
                write_json_atomic(
                    path, owner.attempt_type(identity, day, started, tuple(pages)).value()
                )
        except Exception as error:
            failure = type(error).__name__
            if isinstance(error, (JQuantsRateLimitError, AcquisitionDeadlineExceeded)):
                totals["halted"] = 1
        item = owner.attempt_type(identity, day, started, tuple(pages), failure)
        write_json_atomic(path, item.value())
        attempts.append(item)
        if failure:
            break
    if attempts:
        commit()
    return totals


def build_parser(description: str) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=description)
    parser.add_argument(
        "command", choices=("initialize", "plan", "acquire", "resume", "commit-work")
    )
    parser.add_argument("--directory", type=Path, required=True)
    parser.add_argument("--start", type=date.fromisoformat)
    parser.add_argument("--end", type=date.fromisoformat)
    parser.add_argument("--revision-overlap-days", type=int, default=7)
    return parser


def native_main(
    argv: list[str] | None,
    *,
    description: str,
    owner_type: Callable[[Any, Path], FullDayL1[Any, Any]],
    attempt_type: type[FullDayAttempt],
    store_factory: Callable[..., Any],
    client_factory: Callable[..., JQuantsHTTP],
) -> int:
    args = build_parser(description).parse_args(argv)
    try:
        with closing(
            store_factory(bucket=os.environ.get("R2_STORES_BUCKET") or "baibai-stores")
        ) as store:
            owner = owner_type(store, args.directory)
            if args.command == "initialize":
                print(owner.initialize().content_id)
            elif args.command == "resume":
                print(owner.resume().content_id)
            elif args.command == "commit-work":
                attempts = [
                    attempt_type.from_value(json.loads(path.read_bytes()))
                    for path in args.directory.glob("attempt-*.json")
                ]
                attempts.sort(
                    key=lambda item: (item.attempted_at or item.order_time, item.request_id)
                )
                result = owner.commit(attempts)
                fixed = owner.fixed().value()
                unresolved: dict[str, list[str]] = {}
                for day in sorted({item.day.isoformat() for item in attempts}):
                    scope = fixed["scopes"].get(day, {})
                    reasons = [field for field in ("failure", "held") if scope.get(field)]
                    pending = fixed["pending"]
                    if pending is not None and day in pending["days"]:
                        reasons.append("pending")
                    if reasons:
                        unresolved[day] = reasons
                # Identity replay only confirms persistence. Use the current
                # affected scopes for acquisition success, including retries
                # after failure and old failures resolved by a newer receipt.
                print(json.dumps({**result, "unresolved": unresolved}, sort_keys=True))
                return int(bool(unresolved))
            else:
                if args.start is None or args.end is None:
                    raise ValueError("--start and --end are required")
                if args.command == "plan":
                    days = owner.fixed().plan(
                        start=args.start,
                        end=args.end,
                        revision_overlap_days=args.revision_overlap_days,
                    )
                    print(json.dumps([day.isoformat() for day in days]))
                else:
                    with closing(client_factory(os.environ.get("JQUANTS_API_KEY", ""))) as client:
                        report = acquire_full_day_native(
                            owner,
                            start=args.start,
                            end=args.end,
                            revision_overlap_days=args.revision_overlap_days,
                            client=client,
                        )
                    print(json.dumps(report, sort_keys=True))
                    return int(bool(report["failed"] or report["held"]))
    except Exception as error:
        print(f"full-day native L1 failed: {type(error).__name__}", file=sys.stderr)
        return 1
    return 0
