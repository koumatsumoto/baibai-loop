"""Summarize native market request evidence without reading an L2 projection."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, datetime

from baibai_engine.data_collection.providers.jquants.contracts import JQuantsSource, RetainedRequest


@dataclass(frozen=True, slots=True)
class ReceiptEvidence:
    request_id: str
    source: str
    start: date
    end: date
    received_at: datetime
    records: int
    calendar_days: tuple[date, ...] = ()
    calendar_divisions: tuple[tuple[date, str], ...] = ()
    invalid_calendar_days: tuple[date, ...] = ()
    valid_calendar: bool = True


@dataclass(frozen=True, slots=True)
class IncompleteReceipt:
    request_id: str
    query: tuple[tuple[str, str], ...] | None
    received_at: datetime | None


@dataclass(frozen=True, slots=True)
class NativeMarketIndex:
    source: JQuantsSource
    receipts: tuple[ReceiptEvidence, ...]
    incomplete: tuple[IncompleteReceipt, ...]


def index_market_originals(
    requests: Iterable[RetainedRequest], *, source: JQuantsSource
) -> NativeMarketIndex:
    """Distinguish full explicit day/range responses, valid empty, and incomplete.

    A filtered ticker query does not establish a market-wide receipt. This index
    is derived on demand from L1 and is not stored as a new authoritative layer.
    """
    receipts: list[ReceiptEvidence] = []
    incomplete: list[IncompleteReceipt] = []
    fingerprints: dict[str, dict[int, str]] = {}
    for item in requests:
        saved = fingerprints.setdefault(item.request_id, {})
        for page, fingerprint in item.page_fingerprints:
            existing = saved.setdefault(page, fingerprint)
            if existing != fingerprint:
                raise ValueError("conflicting retained page identity")
        request = item.request
        if request is None:
            incomplete.append(
                IncompleteReceipt(
                    item.request_id,
                    None if item.first_query is None else tuple(sorted(item.first_query.items())),
                    item.last_received_at,
                )
            )
            continue
        if request.source != source:
            raise ValueError("original source does not match its index")
        params = request.params
        try:
            if request.source == "market_calendar" and set(params) == {"from", "to"}:
                first, last = date.fromisoformat(params["from"]), date.fromisoformat(params["to"])
            else:
                key = "disc_date" if request.source == "short_sale_reports" else "date"
                if set(params) != {key}:
                    continue
                first = last = date.fromisoformat(params[key])
        except ValueError:
            incomplete.append(
                IncompleteReceipt(
                    item.request_id, tuple(sorted(params.items())), request.received_at
                )
            )
            continue
        if first > last:
            incomplete.append(
                IncompleteReceipt(
                    item.request_id, tuple(sorted(params.items())), request.received_at
                )
            )
            continue
        rows = request.records()
        calendar_days: set[date] = set()
        calendar_divisions: dict[date, str] = {}
        invalid_days: set[date] = set()
        calendar_valid = True
        if source == "market_calendar":
            for row in rows:
                try:
                    observed = date.fromisoformat(str(row.get("Date", ""))[:10])
                except ValueError:
                    calendar_valid = False
                    continue
                category = row.get("HolDiv")
                if observed < first or observed > last:
                    calendar_valid = False
                elif observed in calendar_days or category is None or not str(category).strip():
                    invalid_days.add(observed)
                else:
                    calendar_days.add(observed)
                    calendar_divisions[observed] = str(category).strip()
        receipts.append(
            ReceiptEvidence(
                item.request_id,
                request.source,
                first,
                last,
                request.received_at,
                len(rows),
                tuple(sorted(calendar_days)),
                tuple(sorted(calendar_divisions.items())),
                tuple(sorted(invalid_days)),
                calendar_valid,
            )
        )
    return NativeMarketIndex(
        source, tuple(receipts), tuple(sorted(incomplete, key=lambda item: item.request_id))
    )


@dataclass(frozen=True, slots=True)
class NativeAcquisitionPlan:
    days: tuple[date, ...]
    unknown_incomplete_requests: tuple[str, ...]


def _day_state(
    index: NativeMarketIndex, *, date_key: str
) -> tuple[dict[date, ReceiptEvidence], set[date], dict[date, datetime | None], list[str]]:
    latest: dict[date, ReceiptEvidence] = {}
    ambiguous: set[date] = set()
    for receipt in index.receipts:
        old = latest.get(receipt.start)
        if old is None or receipt.received_at > old.received_at:
            latest[receipt.start] = receipt
            ambiguous.discard(receipt.start)
        elif receipt.received_at == old.received_at and receipt.request_id != old.request_id:
            # Same-clock requests can differ; a new fetch is safer than guessing.
            ambiguous.add(receipt.start)
    incomplete: dict[date, datetime | None] = {}
    unknown: list[str] = []
    for item in index.incomplete:
        query = dict(item.query or ())
        if set(query) != {date_key}:
            unknown.append(item.request_id)
            continue
        try:
            day = date.fromisoformat(query[date_key])
        except ValueError:
            unknown.append(item.request_id)
            continue
        if day not in incomplete or incomplete[day] is not None:
            previous = incomplete.get(day)
            if item.received_at is None or previous is None or item.received_at > previous:
                incomplete[day] = item.received_at
    return latest, ambiguous, incomplete, unknown


def plan_required_days(
    index: NativeMarketIndex, *, days: Iterable[date], require_nonempty: bool
) -> NativeAcquisitionPlan:
    """Find missing full-scope days without asserting downstream row quality."""
    date_key = "disc_date" if index.source == "short_sale_reports" else "date"
    latest, ambiguous, incomplete, unknown = _day_state(index, date_key=date_key)
    wanted = []
    for day in sorted(set(days)):
        current = latest.get(day)
        partial = incomplete.get(day)
        unresolved = day in incomplete and (
            partial is None or current is None or partial >= current.received_at
        )
        if (
            current is None
            or unresolved
            or day in ambiguous
            or (require_nonempty and not current.records)
        ):
            wanted.append(day)
    return NativeAcquisitionPlan(tuple(wanted), tuple(sorted(unknown)))


def plan_daily_prices(index: NativeMarketIndex, *, start: date, end: date) -> NativeAcquisitionPlan:
    """Plan source-day receipts; distinguish native empty from absent or partial.

    This only establishes native receipt completion, not L2 row quality.
    A newer partial receipt is fetched again even when an older complete one exists.
    """
    from datetime import timedelta

    if index.source != "daily_bars" or start > end:
        raise ValueError("daily price plan requires a valid daily-bars interval")
    latest, ambiguous, incomplete, unknown = _day_state(index, date_key="date")
    days: list[date] = []
    day = start
    while day <= end:
        current = latest.get(day)
        incomplete_at = incomplete.get(day)
        unresolved = day in incomplete and (
            incomplete_at is None or current is None or incomplete_at >= current.received_at
        )
        if current is None or unresolved or day in ambiguous:
            days.append(day)
        day += timedelta(days=1)
    if not days:
        nonempty = [day for day, receipt in latest.items() if receipt.records]
        latest_in_window = max((day for day in nonempty if start <= day <= end), default=None)
        latest_stored = max(nonempty, default=None)
        if (
            latest_in_window is not None
            and latest_in_window < end
            and latest_stored == latest_in_window
        ):
            cursor = latest_in_window
            while cursor <= end:
                days.append(cursor)
                cursor += timedelta(days=1)
    return NativeAcquisitionPlan(tuple(days), tuple(sorted(unknown)))


def plan_calendar(index: NativeMarketIndex, *, start: date, end: date) -> NativeAcquisitionPlan:
    """Re-request only absent, incomplete or ambiguous native calendar dates."""
    from datetime import timedelta

    if index.source != "market_calendar" or start > end:
        raise ValueError("calendar plan requires a valid calendar interval")
    latest: dict[date, tuple[datetime, bool]] = {}
    for item in index.receipts:
        available = set(item.calendar_days) - set(item.invalid_calendar_days)
        cursor = max(start, item.start)
        stop = min(end, item.end)
        while cursor <= stop:
            valid = item.valid_calendar and cursor in available
            old = latest.get(cursor)
            if old is None or item.received_at > old[0]:
                latest[cursor] = item.received_at, valid
            elif item.received_at == old[0] and valid != old[1]:
                latest[cursor] = item.received_at, False
            cursor += timedelta(days=1)
    unknown: list[str] = []
    partial: dict[date, datetime | None] = {}
    for failed in index.incomplete:
        params = dict(failed.query or ())
        try:
            if set(params) != {"from", "to"}:
                raise ValueError("unknown scope")
            first, last = date.fromisoformat(params["from"]), date.fromisoformat(params["to"])
            if first > last:
                raise ValueError("reversed scope")
        except ValueError:
            unknown.append(failed.request_id)
            continue
        cursor = max(start, first)
        stop = min(end, last)
        while cursor <= stop:
            previous_partial = partial.get(cursor)
            if cursor not in partial or (
                previous_partial is not None
                and (failed.received_at is None or failed.received_at > previous_partial)
            ):
                partial[cursor] = failed.received_at
            cursor += timedelta(days=1)
    missing: list[date] = []
    cursor = start
    while cursor <= end:
        receipt = latest.get(cursor)
        incomplete_at = partial.get(cursor)
        unresolved = cursor in partial and (
            incomplete_at is None or receipt is None or incomplete_at >= receipt[0]
        )
        if receipt is None or not receipt[1] or unresolved:
            missing.append(cursor)
        cursor += timedelta(days=1)
    return NativeAcquisitionPlan(tuple(missing), tuple(sorted(unknown)))


def calendar_sessions(
    index: NativeMarketIndex, *, start: date, end: date
) -> tuple[dict[date, bool | None], tuple[str, ...]]:
    """Read known cash-equity sessions from originals; unknown stays unknown."""
    from datetime import timedelta

    plan = plan_calendar(index, start=start, end=end)
    unavailable = set(plan.days)
    latest: dict[date, tuple[datetime, str | None]] = {}
    for item in index.receipts:
        for day, division in item.calendar_divisions:
            if not start <= day <= end or day in unavailable:
                continue
            old = latest.get(day)
            if old is None or item.received_at > old[0]:
                latest[day] = item.received_at, division
            elif item.received_at == old[0] and division != old[1]:
                latest[day] = item.received_at, None
    result: dict[date, bool | None] = {}
    day = start
    while day <= end:
        value = latest.get(day)
        category = value[1] if value is not None else None
        result[day] = category in {"1", "2"} if category is not None else None
        day += timedelta(days=1)
    return result, plan.unknown_incomplete_requests


@dataclass(frozen=True, slots=True)
class MarginAcquisitionPlan:
    days: tuple[date, ...]
    unknown_calendar_days: tuple[date, ...]
    unknown_incomplete_requests: tuple[str, ...]


def plan_daily_margin(
    margin: NativeMarketIndex,
    calendar: NativeMarketIndex,
    *,
    first_balance_date: date,
    asof: date,
) -> MarginAcquisitionPlan:
    """Plan published daily balance dates using only native source receipts."""
    if margin.source != "margin_interest" or calendar.source != "market_calendar":
        raise ValueError("margin plan requires native margin and calendar indexes")
    if asof < first_balance_date:
        return MarginAcquisitionPlan((), (), ())
    sessions, calendar_unknown = calendar_sessions(calendar, start=first_balance_date, end=asof)
    trading = [day for day, is_open in sessions.items() if is_open is True]
    # One later session is required before a daily balance is due for publication.
    candidates = trading[:-1]
    plan = plan_required_days(margin, days=candidates, require_nonempty=True)
    return MarginAcquisitionPlan(
        plan.days,
        tuple(day for day, state in sessions.items() if state is None),
        tuple(sorted(set(calendar_unknown) | set(plan.unknown_incomplete_requests))),
    )


def plan_weekly_margin(
    margin: NativeMarketIndex,
    calendar: NativeMarketIndex,
    *,
    start: date,
    asof: date,
    final_balance_date: date,
    final_publication_date: date,
) -> MarginAcquisitionPlan:
    """Plan the legacy weekly source using its native two-session release lag."""
    from datetime import timedelta, timezone

    if margin.source != "margin_interest" or calendar.source != "market_calendar":
        raise ValueError("margin plan requires native margin and calendar indexes")
    if start > asof or start > final_balance_date:
        return MarginAcquisitionPlan((), (), ())
    sessions, calendar_unknown = calendar_sessions(calendar, start=start, end=asof)
    trading = [day for day, is_open in sessions.items() if is_open is True]
    balance_end = min(asof, final_balance_date)
    weeks: dict[tuple[int, int], list[date]] = {}
    for day in sessions:
        if day <= balance_end:
            iso = day.isocalendar()
            weeks.setdefault((iso.year, iso.week), []).append(day)
    candidates: list[date] = []
    publication: dict[date, date] = {}
    for days in weeks.values():
        if any(sessions[day] is None for day in days):
            continue
        open_days = [day for day in days if sessions[day] is True]
        if not open_days:
            continue
        balance = open_days[-1]
        later = [day for day in trading if day > balance]
        if len(later) >= 2:
            candidates.append(balance)
            publication[balance] = later[1]
    planned = plan_required_days(margin, days=candidates, require_nonempty=False)
    latest, ambiguous, _, _ = _day_state(margin, date_key="date")
    refresh = set(planned.days)
    jst = timezone(timedelta(hours=9))
    for day in candidates:
        receipt = latest.get(day)
        if receipt is None or receipt.records or day in ambiguous:
            continue
        if receipt.received_at.astimezone(jst).date() <= publication[day] or (
            day == final_balance_date and asof >= final_publication_date
        ):
            refresh.add(day)
    return MarginAcquisitionPlan(
        tuple(sorted(refresh)),
        tuple(day for day, state in sessions.items() if state is None),
        tuple(sorted(set(calendar_unknown) | set(planned.unknown_incomplete_requests))),
    )
