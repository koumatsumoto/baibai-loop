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
        receipts.append(
            ReceiptEvidence(
                item.request_id,
                request.source,
                first,
                last,
                request.received_at,
                len(rows),
                tuple(sorted(calendar_days)),
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


def plan_daily_prices(index: NativeMarketIndex, *, start: date, end: date) -> NativeAcquisitionPlan:
    """Plan source-day receipts; distinguish native empty from absent or partial.

    This only establishes native receipt completion, not L2 row quality.
    A newer partial receipt is fetched again even when an older complete one exists.
    """
    from datetime import timedelta

    if index.source != "daily_bars" or start > end:
        raise ValueError("daily price plan requires a valid daily-bars interval")
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
        if set(query) != {"date"}:
            unknown.append(item.request_id)
            continue
        try:
            day = date.fromisoformat(query["date"])
        except ValueError:
            unknown.append(item.request_id)
            continue
        if day not in incomplete or incomplete[day] is not None:
            previous = incomplete.get(day)
            if item.received_at is None or previous is None or item.received_at > previous:
                incomplete[day] = item.received_at
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
