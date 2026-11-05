"""Synthetic designated-issue margin publications; no provider or production data."""

from datetime import UTC, date, datetime

from baibai_engine.data_collection.providers.jquants.contracts import JQuantsPage
from baibai_engine.data_collection.providers.jquants.margin_alerts_native import MarginAlertsAttempt
from tests.helpers.jquants_receipts import body, source_record

DAY = date(2026, 11, 2)
AT = datetime(2026, 11, 3, tzinfo=UTC)


def records(day=DAY, *, application=None, long=2000):
    record = source_record("margin_alerts", day.isoformat())
    return [{**record, "AppDate": (application or day).isoformat(), "LongOut": long}]


def attempt(identity, *, day=DAY, at=AT, rows=None, partial=False):
    return MarginAlertsAttempt(
        identity,
        day,
        at,
        (
            JQuantsPage(
                "margin_alerts",
                {"date": day.isoformat()},
                at,
                body(records(day) if rows is None else rows, cursor="next" if partial else None),
            ),
        ),
        "interrupted" if partial else None,
    )
