"""Synthetic investor-level short reports; no provider or production data."""

from datetime import UTC, date, datetime

from baibai_engine.data_collection.providers.jquants.contracts import JQuantsPage
from baibai_engine.data_collection.providers.jquants.short_sale_reports_native import (
    ShortSaleReportsAttempt,
)
from tests.helpers.jquants_receipts import body, source_record

DAY = date(2026, 11, 2)
AT = datetime(2026, 11, 3, tzinfo=UTC)


def records(day=DAY, *, calculated=None, ratio=0.006, name="Reporter A"):
    record = source_record("short_sale_reports", day.isoformat())
    return [
        {
            **record,
            "CalcDate": (calculated or day).isoformat(),
            "ShrtPosToSO": ratio,
            "SSName": name,
        }
    ]


def attempt(identity, *, day=DAY, at=AT, rows=None, partial=False):
    return ShortSaleReportsAttempt(
        identity,
        day,
        at,
        (
            JQuantsPage(
                "short_sale_reports",
                {"disc_date": day.isoformat()},
                at,
                body(records(day) if rows is None else rows, cursor="next" if partial else None),
            ),
        ),
        "interrupted" if partial else None,
    )
