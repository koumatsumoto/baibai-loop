"""Cash-equity session date boundaries, separate from source observation times."""

from __future__ import annotations

from datetime import date, datetime, time, timedelta

from baibai_engine.primitives.time import JST


def cash_equity_close_time(day: date) -> time:
    """Use the scheduled close for the session, not today's trading hours.

    JPX extended the cash-equity afternoon session on 2024-11-05.
    https://www.jpx.co.jp/corporate/news/news-releases/1030/20241105-01.html
    """
    return time(15, 30) if day >= date(2024, 11, 5) else time(15)


def latest_eligible_close_date(at: datetime) -> date:
    """Apply the JST session cutoff without inventing a source observation timestamp."""
    if at.tzinfo is None or at.utcoffset() is None:
        raise ValueError("market instant requires a timezone")
    local = at.astimezone(JST)
    return local.date() - timedelta(days=local.time() < cash_equity_close_time(local.date()))
