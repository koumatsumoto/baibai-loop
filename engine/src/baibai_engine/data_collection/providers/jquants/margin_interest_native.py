"""All-issues margin originals, with source-specific weekly/daily fulfillment."""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from baibai_engine.data_collection.providers.jquants.calendar_native import FixedCalendarL1
from baibai_engine.data_collection.providers.jquants.contracts import RetainedRequest
from baibai_engine.data_collection.providers.jquants.full_day_native import (
    FixedFullDayL1,
    FullDayAttempt,
    FullDayContract,
    FullDayL1,
    FullDayObjectStore,
    read_full_day_requests,
)
from baibai_engine.data_collection.providers.jquants.market_rules import (
    ALL_ISSUES_DAILY_FIRST_BALANCE_DATE,
    LEGACY_WEEKLY_LAST_BALANCE_DATE,
    LEGACY_WEEKLY_LAST_PUBLICATION_DATE,
    WEEKLY_BALANCE_LOOKBACK_DAYS,
)
from baibai_engine.data_collection.providers.jquants.native_index import (
    IncompleteReceipt,
    MarginAcquisitionPlan,
    NativeMarketIndex,
    ReceiptEvidence,
    calendar_sessions,
    plan_daily_margin,
    plan_required_days,
    plan_weekly_margin,
)
from baibai_engine.storage.objects.r2 import ObjectStore

PREFIX = "l1/jquants/margin-interest/v1/"
HEAD_KEY = PREFIX + "head.json"
FORMAT = "jquants-margin-interest-native-v1"
_CONTRACT = FullDayContract("margin_interest", PREFIX, FORMAT)


class MarginInterestAttempt(FullDayAttempt):
    source = "margin_interest"


class FixedMarginInterestL1(FixedFullDayL1):
    contract = _CONTRACT
    attempt_type = MarginInterestAttempt

    def index(self) -> NativeMarketIndex:
        """Use real confirmations, not retimed pages or L2 row counts."""
        receipts, failures = [], []
        for raw_day, scope in sorted(self.value()["scopes"].items()):
            day = date.fromisoformat(raw_day)
            if identity := scope.get("adopted"):
                receipts.append(
                    ReceiptEvidence(
                        identity,
                        "margin_interest",
                        day,
                        day,
                        datetime.fromisoformat(scope["confirmed_at"]),
                        int(scope["record_count"]),
                    )
                )
            if identity := scope.get("failure") or scope.get("held"):
                failures.append(
                    IncompleteReceipt(
                        identity,
                        (("date", raw_day),),
                        datetime.fromisoformat(scope["last_attempt_at"]),
                    )
                )
        return NativeMarketIndex("margin_interest", tuple(receipts), tuple(failures))


class MarginInterestL1(FullDayL1[MarginInterestAttempt, FixedMarginInterestL1]):
    def __init__(self, store: FullDayObjectStore, directory: Path) -> None:
        super().__init__(
            store,
            directory,
            contract=_CONTRACT,
            attempt_type=MarginInterestAttempt,
            fixed_type=FixedMarginInterestL1,
            max_pack_bytes=lambda: 16 * 1024 * 1024,
        )


def balance_days(
    calendar: FixedCalendarL1, *, start: date, end: date
) -> tuple[tuple[date, ...], tuple[date, ...], tuple[str, ...]]:
    """Finite backfill includes its last balance day, unlike a live publication plan."""
    if start > end:
        raise ValueError("invalid margin balance interval")
    sessions, unknown = calendar_sessions(calendar.index(), start=start, end=end)
    weeks: dict[tuple[int, int], list[date]] = {}
    daily = []
    for day, opened in sessions.items():
        if day <= LEGACY_WEEKLY_LAST_BALANCE_DATE:
            weeks.setdefault(day.isocalendar()[:2], []).append(day)
        elif day >= ALL_ISSUES_DAILY_FIRST_BALANCE_DATE and opened is True:
            daily.append(day)
    weekly = []
    for days in weeks.values():
        if any(sessions[day] is None for day in days):
            continue
        opened_days = [day for day in days if sessions[day] is True]
        if opened_days:
            weekly.append(opened_days[-1])
    return (
        tuple(sorted(weekly + daily)),
        tuple(day for day, opened in sessions.items() if opened is None),
        unknown,
    )


def plan_margin_interest_days(
    fixed: FixedMarginInterestL1,
    calendar: FixedCalendarL1,
    *,
    asof: date,
    start: date | None = None,
    end: date | None = None,
    force: bool = False,
) -> MarginAcquisitionPlan:
    """Keep the existing 230-day weekly and full post-transition daily plan.

    A paired start/end is an explicit historical backfill, including the endpoint.
    It may recheck adopted dates with force; normal acquisition never turns L2
    projection quality into a fetch condition. The calendar is fixed by the caller.
    """
    if (start is None) != (end is None) or (force and start is None):
        raise ValueError("backfill requires --start and --end; force requires a finite backfill")
    if end is not None and (start is None or start > end or end > asof):
        raise ValueError("invalid historical margin interval")
    index = NativeMarketIndex("margin_interest", (), ()) if force else fixed.index()
    weekly = plan_weekly_margin(
        index,
        calendar.index(),
        start=start or asof - timedelta(days=WEEKLY_BALANCE_LOOKBACK_DAYS),
        asof=asof,
        final_balance_date=LEGACY_WEEKLY_LAST_BALANCE_DATE,
        final_publication_date=LEGACY_WEEKLY_LAST_PUBLICATION_DATE,
    )
    if start is not None and end is not None:
        candidates, missing, unknown = balance_days(calendar, start=start, end=end)
        old = plan_required_days(
            index,
            days=[day for day in candidates if day <= LEGACY_WEEKLY_LAST_BALANCE_DATE],
            require_nonempty=False,
        )
        new = plan_required_days(
            index,
            days=[day for day in candidates if day >= ALL_ISSUES_DAILY_FIRST_BALANCE_DATE],
            require_nonempty=True,
        )
        return MarginAcquisitionPlan(
            tuple(sorted(set(old.days) | set(new.days) | {d for d in weekly.days if d <= end})),
            missing,
            tuple(
                sorted(
                    set(unknown)
                    | set(old.unknown_incomplete_requests)
                    | set(new.unknown_incomplete_requests)
                )
            ),
        )
    daily = plan_daily_margin(
        index,
        calendar.index(),
        first_balance_date=ALL_ISSUES_DAILY_FIRST_BALANCE_DATE,
        asof=asof,
    )
    return MarginAcquisitionPlan(
        tuple(sorted(set(weekly.days) | set(daily.days))),
        tuple(sorted(set(weekly.unknown_calendar_days) | set(daily.unknown_calendar_days))),
        tuple(
            sorted(set(weekly.unknown_incomplete_requests) | set(daily.unknown_incomplete_requests))
        ),
    )


def read_margin_interest_requests(
    store: ObjectStore, *, scopes: dict[str, Any], receipts: dict[str, Any], days: Sequence[date]
) -> Iterator[RetainedRequest]:
    return read_full_day_requests(
        store,
        scopes=scopes,
        receipts=receipts,
        days=days,
        contract=_CONTRACT,
        attempt_type=MarginInterestAttempt,
    )
