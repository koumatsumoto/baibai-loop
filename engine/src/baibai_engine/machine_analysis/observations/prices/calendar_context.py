"""Resolve exact observation master basis from the saved cash-equity calendar."""

import sqlite3
from contextlib import closing
from datetime import date
from pathlib import Path

from baibai_engine.storage.sqlite.source_cache.read import connect_read_only


def observation_calendar_context(conn: sqlite3.Connection, day: date) -> tuple[date, bool]:
    """Resolve the required master date without consulting TradingView L2 rows."""
    calendar = conn.execute(
        "SELECT holiday_division IN ('1', '2') FROM jquants_market_calendar WHERE day=?",
        (day.isoformat(),),
    ).fetchone()
    if calendar is None:
        raise ValueError("Exact-date trading calendar is unavailable")
    business = bool(calendar[0])
    if business:
        basis = day
    else:
        previous = conn.execute(
            "SELECT MAX(day) FROM jquants_market_calendar "
            "WHERE day<? AND holiday_division IN ('1', '2')",
            (day.isoformat(),),
        ).fetchone()[0]
        if previous is None:
            raise ValueError("Previous business day is unavailable in trading calendar")
        basis = date.fromisoformat(previous)
        calendar_days = conn.execute(
            "SELECT COUNT(*) FROM jquants_market_calendar WHERE day BETWEEN ? AND ?",
            (basis.isoformat(), day.isoformat()),
        ).fetchone()[0]
        if calendar_days != (day - basis).days + 1:
            raise ValueError("Continuous trading calendar coverage is unavailable")
    return basis, business


def market_calendar_business_day(path: Path, day: date) -> bool | None:
    """Return whether ``day`` is a trading day, or None when the calendar has no such row.

    Calendar read failures raise rather than being treated as a closed market.
    An absent file, an unreadable store and
    an uncovered date remain distinct from a confirmed non-business day.
    """

    with closing(connect_read_only(path)) as connection:
        row = connection.execute(
            "SELECT holiday_division IN ('1', '2') FROM jquants_market_calendar WHERE day = ?",
            (day.isoformat(),),
        ).fetchone()
    return None if row is None else bool(row[0])


def read_price_calendar(connection: sqlite3.Connection, *, as_of: date) -> dict[date, bool]:
    """Share the saved calendar without applying another indicator's history requirement."""
    return {
        date.fromisoformat(row[0]): bool(row[1])
        for row in connection.execute(
            "SELECT day, holiday_division IN ('1','2') FROM jquants_market_calendar "
            "WHERE day<=? ORDER BY day",
            (as_of.isoformat(),),
        )
    }
