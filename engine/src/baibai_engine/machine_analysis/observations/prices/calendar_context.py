"""Resolve exact observation master basis from the saved cash-equity calendar."""

import sqlite3
from collections.abc import Mapping, Sequence
from datetime import date, timedelta

from baibai_engine.primitives.jquants_calendar import (
    CASH_EQUITY_KNOWN_DIVISIONS,
    CASH_EQUITY_OPEN_DIVISIONS,
    cash_equity_open,
)


def observation_calendar_context(conn: sqlite3.Connection, day: date) -> tuple[date, bool]:
    """Resolve the required master date without consulting TradingView L2 rows."""
    calendar = conn.execute(
        "SELECT holiday_division FROM jquants_market_calendar WHERE day=?",
        (day.isoformat(),),
    ).fetchone()
    business = None if calendar is None else cash_equity_open(calendar[0])
    if business is None:
        raise ValueError("Exact-date trading calendar is unavailable")
    if business:
        basis = day
    else:
        previous = conn.execute(
            "SELECT MAX(day) FROM jquants_market_calendar "
            "WHERE day<? AND holiday_division IN (?, ?)",
            (day.isoformat(), *CASH_EQUITY_OPEN_DIVISIONS),
        ).fetchone()[0]
        if previous is None:
            raise ValueError("Previous business day is unavailable in trading calendar")
        basis = date.fromisoformat(previous)
        calendar_days = conn.execute(
            "SELECT COUNT(*) FROM jquants_market_calendar "
            "WHERE day BETWEEN ? AND ? AND holiday_division IN (?, ?, ?, ?)",
            (basis.isoformat(), day.isoformat(), *CASH_EQUITY_KNOWN_DIVISIONS),
        ).fetchone()[0]
        if calendar_days != (day - basis).days + 1:
            raise ValueError("Continuous trading calendar coverage is unavailable")
    return basis, business


def read_price_calendar(connection: sqlite3.Connection, *, as_of: date) -> dict[date, bool]:
    """Share the saved calendar without applying another indicator's history requirement."""
    return {
        date.fromisoformat(row[0]): bool(row[1])
        for row in connection.execute(
            "SELECT day, holiday_division IN (?, ?) FROM jquants_market_calendar "
            "WHERE day<=? AND holiday_division IN (?, ?, ?, ?) ORDER BY day",
            (*CASH_EQUITY_OPEN_DIVISIONS, as_of.isoformat(), *CASH_EQUITY_KNOWN_DIVISIONS),
        )
    }


def confirmed_closed_master_bases(
    calendar: Mapping[date, str], *, coverage: Sequence[tuple[date, date]]
) -> dict[date, date]:
    """Known closed sessions may use the preceding verified cash-equity session.

    Unknown classifications, gaps and unconfirmed coverage never authorize a
    date adjustment. Acquisition itself does not depend on this L2 selection.
    """
    result: dict[date, date] = {}
    previous: date | None = None
    basis: date | None = None
    for day, division in sorted(calendar.items()):
        business = cash_equity_open(division)
        if business is True:
            basis = day
        elif business is None or previous != day - timedelta(days=1):
            basis = None
        elif basis is not None and any(start <= basis <= day <= end for start, end in coverage):
            result[day] = basis
        previous = day
    return result
