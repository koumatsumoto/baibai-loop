"""固定margin原本と暦から有限な週次・日次観測copyを作る。歴史評価規則は変更しない。"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from contextlib import closing
from datetime import UTC, date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from pydantic import JsonValue

from baibai_engine.data_collection.providers.jquants.calendar_native import (
    CalendarL1,
    FixedCalendarL1,
)
from baibai_engine.data_collection.providers.jquants.margin_interest_native import (
    FixedMarginInterestL1,
    MarginInterestL1,
    balance_days,
    read_margin_interest_requests,
)
from baibai_engine.data_collection.providers.jquants.market_rules import (
    ALL_ISSUES_DAILY_FIRST_BALANCE_DATE,
    LEGACY_WEEKLY_LAST_BALANCE_DATE,
    LEGACY_WEEKLY_LAST_PUBLICATION_DATE,
)
from baibai_engine.machine_analysis.generation.contracts import InputSnapshot
from baibai_engine.machine_analysis.generation.margin_interest_inputs import (
    INPUT_FORMAT,
    read_margin_interest_input,
    retain_margin_interest_input,
)
from baibai_engine.machine_analysis.observations.jquants_writer import (
    store_jquants_all_issues_daily_margin,
    store_jquants_weekly_margin,
)
from baibai_engine.machine_analysis.observations.trading_activity.jquants_contract import (
    ALL_ISSUES_DAILY_MARGIN_SOURCE,
    WEEKLY_MARGIN_SOURCE,
    all_issues_daily_margin_coverage_key,
    weekly_margin_coverage_key,
)
from baibai_engine.primitives.json import canonical_json
from baibai_engine.storage.json import write_json_atomic
from baibai_engine.storage.objects.r2 import Boto3R2Store, ObjectStore
from baibai_engine.storage.sqlite.source_cache.coverage import record_source_coverage
from baibai_engine.storage.sqlite.source_cache.schema import open_connection
from baibai_jobs.integration.calendar_native import fixed_input as fixed_calendar_input
from baibai_jobs.integration.calendar_native import materialize_saved_input as materialize_calendar


def _days(start: date, end: date) -> tuple[date, ...]:
    if start > end:
        raise ValueError("invalid margin balance interval")
    return tuple(start + timedelta(days=i) for i in range((end - start).days + 1))


def _validate_clock(scopes: dict[str, Any], evaluated_at: datetime) -> None:
    if evaluated_at.utcoffset() is None:
        raise ValueError("evaluation time requires a timezone")
    for scope in scopes.values():
        for field in ("confirmed_at", "last_attempt_at", "changed_at"):
            if stamp := scope.get(field):
                parsed = datetime.fromisoformat(stamp)
                if parsed.utcoffset() is None or parsed > evaluated_at:
                    raise ValueError("margin state is later than the evaluation clock")


def fixed_input(
    store: ObjectStore,
    fixed: FixedMarginInterestL1,
    calendar: FixedCalendarL1,
    *,
    start: date,
    end: date,
    evaluated_at: datetime,
) -> InputSnapshot:
    """Fix originals and expected balance dates; do not invent historical vintages."""
    days = _days(start, end)
    head = fixed.value()
    scopes = {day.isoformat(): head["scopes"].get(day.isoformat(), {}) for day in days}
    _validate_clock(scopes, evaluated_at)
    expected, unknown, incomplete = balance_days(calendar, start=start, end=end)
    saved_calendar = fixed_calendar_input(
        store, calendar, as_of=end, evaluated_at=evaluated_at, days=days
    )
    summary: dict[str, Any] = {
        "selection_start": start.isoformat(),
        "selection_end": end.isoformat(),
        "evaluated_at": evaluated_at.isoformat(),
        "expected_days": [day.isoformat() for day in expected],
        "unknown_calendar_days": [day.isoformat() for day in unknown],
        "unknown_calendar_requests": list(incomplete),
        "calendar_input": saved_calendar.model_dump(mode="json"),
    }
    descriptor = retain_margin_interest_input(
        store,
        {
            "head_sha256": fixed.content_id,
            "as_of": end.isoformat(),
            "scopes": scopes,
            "receipts": head["receipts"],
            "summary": summary,
        },
    )
    digest = hashlib.sha256(canonical_json(descriptor).encode()).hexdigest()
    return InputSnapshot(
        kind="native_l1",
        reference="jquants-margin-interest-" + digest,
        sha256=digest,
        descriptor=descriptor,
    )


def _descriptor(saved: InputSnapshot) -> dict[str, JsonValue]:
    saved = InputSnapshot.model_validate_json(saved.model_dump_json())
    if (
        saved.kind != "native_l1"
        or saved.descriptor is None
        or saved.descriptor.get("format") != INPUT_FORMAT
    ):
        raise ValueError("a digest-verified native margin interest input is required")
    return saved.descriptor


def saved_interval(saved: InputSnapshot) -> tuple[date, date]:
    summary = _descriptor(saved)["summary"]
    if not isinstance(summary, dict):
        raise ValueError("margin input requires its selection summary")
    first, last = summary.get("selection_start"), summary.get("selection_end")
    if not isinstance(first, str) or not isinstance(last, str):
        raise ValueError("margin input requires its balance interval")
    start, end = date.fromisoformat(first), date.fromisoformat(last)
    if start > end:
        raise ValueError("invalid saved margin interval")
    return start, end


def _coverage(day: date) -> tuple[str, str] | None:
    if day <= LEGACY_WEEKLY_LAST_BALANCE_DATE:
        return WEEKLY_MARGIN_SOURCE, weekly_margin_coverage_key(day)
    if day >= ALL_ISSUES_DAILY_FIRST_BALANCE_DATE:
        return (
            ALL_ISSUES_DAILY_MARGIN_SOURCE,
            all_issues_daily_margin_coverage_key(day),
        )
    return None


def materialize_saved_input(
    store: ObjectStore,
    saved: InputSnapshot,
    *,
    output: Path,
    start: date | None = None,
    end: date | None = None,
) -> dict[str, Any]:
    """Cold-rebuild a new view. No price bars, analytical cutoff or old model is changed."""
    first, last = saved_interval(saved)
    start, end = start or first, end or last
    if not first <= start <= end <= last:
        raise ValueError("requested interval is outside the saved margin selection")
    days = _days(start, end)
    descriptor = _descriptor(saved)
    summary = descriptor["summary"]
    if not isinstance(summary, dict):
        raise ValueError("margin input requires its selection summary")
    stamp = summary.get("evaluated_at")
    raw_expected = summary.get("expected_days")
    if not isinstance(stamp, str) or not isinstance(raw_expected, list):
        raise ValueError("margin input requires its evaluation clock and expected dates")
    if not all(isinstance(day, str) for day in raw_expected):
        raise ValueError("invalid expected margin dates")
    evaluated_at = datetime.fromisoformat(stamp)
    final_week_due = (
        evaluated_at.astimezone(timezone(timedelta(hours=9))).date()
        >= LEGACY_WEEKLY_LAST_PUBLICATION_DATE
    )
    calendar = InputSnapshot.model_validate(summary["calendar_input"])
    selected = read_margin_interest_input(store, descriptor, days=days)
    scopes = selected["scopes"]
    _validate_clock(scopes, evaluated_at)
    expected = {
        day
        for day in raw_expected
        if isinstance(day, str) and start.isoformat() <= day <= end.isoformat()
    }
    adopted = {scope["adopted"]: day for day, scope in scopes.items() if scope.get("adopted")}
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("xb"):
        pass
    try:
        with closing(open_connection(output)):
            pass
        calendar_result = materialize_calendar(store, calendar, market=output, days=days)
        with closing(open_connection(output)) as connection:
            connection.execute("BEGIN")
            found, unsupported = set(), set()
            for item in read_margin_interest_requests(
                store, scopes=scopes, receipts=selected["receipts"], days=days
            ):
                if item.last_received_at is not None and item.last_received_at > evaluated_at:
                    raise ValueError("margin original is later than the evaluation clock")
                if (day := adopted.get(item.request_id)) is None:
                    continue
                if item.request is None or item.error is not None:
                    raise ValueError("adopted margin original is incomplete")
                request = item.request
                if (
                    request.source != "margin_interest"
                    or set(request.params) != {"date"}
                    or date.fromisoformat(request.params["date"]) != date.fromisoformat(day)
                ):
                    raise ValueError("margin original differs from its fixed balance day")
                found.add(item.request_id)
                balance = date.fromisoformat(day)
                if _coverage(balance) is None:
                    unsupported.add(day)
                    continue
                fetched_at = scopes[day].get("confirmed_at") or request.received_at.isoformat()
                if balance <= LEGACY_WEEKLY_LAST_BALANCE_DATE:
                    store_jquants_weekly_margin(
                        output,
                        request.records(),
                        week_end=balance,
                        fetched_at_utc=fetched_at,
                        connection=connection,
                    )
                else:
                    store_jquants_all_issues_daily_margin(
                        output,
                        request.records(),
                        balance_date=balance,
                        fetched_at_utc=fetched_at,
                        connection=connection,
                    )
            if found != set(adopted):
                raise ValueError("margin selection lacks an adopted original")
            required = expected | {day for day, scope in scopes.items() if scope}
            unresolved = []
            for day in sorted(required):
                scope = scopes[day]
                contract = _coverage(date.fromisoformat(day))
                if contract is None:
                    unsupported.add(day)
                    continue
                source, key = contract
                if not scope.get("adopted") or scope.get("failure") or scope.get("held"):
                    query = (
                        "SELECT COUNT(*) FROM jquants_weekly_margin WHERE week_end=?"
                        if source == WEEKLY_MARGIN_SOURCE
                        else "SELECT COUNT(*) FROM jquants_all_issues_daily_margin "
                        "WHERE balance_date=?"
                    )
                    count = connection.execute(query, (day,)).fetchone()[0]
                    record_source_coverage(
                        connection,
                        source=source,
                        coverage_key=key,
                        coverage_start=day,
                        coverage_end=day,
                        record_count=count,
                        status="partial" if scope.get("adopted") else "unavailable",
                        error="native acquisition unresolved",
                        fetched_at_utc=scope.get("last_attempt_at") or evaluated_at.isoformat(),
                    )
                row = connection.execute(
                    "SELECT status,record_count FROM source_coverage "
                    "WHERE source=? AND coverage_key=?",
                    (source, key),
                ).fetchone()
                if (
                    row is None
                    or row[0] != "ok"
                    or (source == ALL_ISSUES_DAILY_MARGIN_SOURCE and not row[1])
                    or (
                        day == LEGACY_WEEKLY_LAST_BALANCE_DATE.isoformat()
                        and final_week_due
                        and not scope.get("record_count")
                    )
                ):
                    unresolved.append(day)
            result = {
                "weekly_rows": connection.execute(
                    "SELECT COUNT(*) FROM jquants_weekly_margin"
                ).fetchone()[0],
                "daily_rows": connection.execute(
                    "SELECT COUNT(*) FROM jquants_all_issues_daily_margin"
                ).fetchone()[0],
                "expected_balance_days": len(expected),
                "unresolved_days": unresolved,
                "unsupported_days": sorted(unsupported),
                "calendar_unavailable_days": calendar_result["unavailable_days"],
                "coverage_status": "partial"
                if unresolved or unsupported or calendar_result["coverage_status"] != "complete"
                else "complete",
                "input_sha256": saved.sha256,
            }
            connection.commit()
    except BaseException:
        output.unlink(missing_ok=True)
        raise
    return result


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--saved-input", type=Path)
    parser.add_argument("--calendar-head", type=Path, help="保存したcalendar-fixed.jsonを使う")
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
            if args.saved_input:
                saved = InputSnapshot.model_validate_json(args.saved_input.read_bytes())
            else:
                calendar = (
                    FixedCalendarL1(args.calendar_head.read_bytes(), "saved")
                    if args.calendar_head
                    else CalendarL1(store, args.work_directory / "calendar").fixed()
                )
                saved = fixed_input(
                    store,
                    MarginInterestL1(store, args.work_directory).fixed(),
                    calendar,
                    start=args.start,
                    end=args.end,
                    evaluated_at=datetime.now(UTC),
                )
            args.output.mkdir(parents=True, exist_ok=False)
            result = materialize_saved_input(
                store,
                saved,
                output=args.output / "margin-interest.sqlite",
                start=args.start,
                end=args.end,
            )
            write_json_atomic(args.output / "input.json", saved.model_dump(mode="json"))
            write_json_atomic(args.output / "result.json", result)
            print(json.dumps(result, sort_keys=True))
            return int(result["coverage_status"] != "complete")
    except Exception as error:
        print(f"margin interest native reader failed: {type(error).__name__}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
