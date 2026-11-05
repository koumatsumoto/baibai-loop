"""Synthetic all-issues margin and source calendar; no production observations."""

from datetime import UTC, date, datetime, timedelta

from baibai_engine.data_collection.providers.jquants.calendar_native import (
    CalendarAttempt,
    CalendarL1,
)
from baibai_engine.data_collection.providers.jquants.contracts import JQuantsPage
from baibai_engine.data_collection.providers.jquants.margin_interest_native import (
    MarginInterestAttempt,
)
from tests.helpers.jquants_receipts import body, source_record

DAY = date(2026, 9, 25)
AT = datetime(2026, 10, 1, tzinfo=UTC)


def records(day=DAY, *, long=2000, publication=None):
    row = {**source_record("margin_interest", day.isoformat()), "LongVol": long}
    if publication is not None:
        row["PubDate"] = publication.isoformat()
    return [row]


def attempt(identity, *, day=DAY, at=AT, rows=None, partial=False, query=None):
    return MarginInterestAttempt(
        identity,
        day,
        at,
        (
            JQuantsPage(
                "margin_interest",
                query or {"date": day.isoformat()},
                at,
                body(records(day) if rows is None else rows, cursor="next" if partial else None),
            ),
        ),
        "interrupted" if partial else None,
    )


def calendar(store, directory, start, end, *, at=AT - timedelta(days=1), omit=(), closed=()):
    days = [start + timedelta(days=i) for i in range((end - start).days + 1)]
    rows = [
        {"Date": d.isoformat(), "HolDiv": "1" if d.weekday() < 5 and d not in closed else "0"}
        for d in days
        if d not in omit
    ]
    owner = CalendarL1(store, directory)
    owner.initialize()
    owner.commit(
        [
            CalendarAttempt(
                "calendar",
                start,
                end,
                at,
                (
                    JQuantsPage(
                        "market_calendar",
                        {"from": start.isoformat(), "to": end.isoformat()},
                        at,
                        body(rows),
                    ),
                ),
            )
        ]
    )
    return owner.fixed()
