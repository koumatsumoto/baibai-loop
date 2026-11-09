"""Resolve exact observation master basis from the saved cash-equity calendar."""

import sqlite3
from collections.abc import Mapping, Sequence
from contextlib import closing
from datetime import date, timedelta
from pathlib import Path

from baibai_engine.storage.sqlite.source_cache.read import connect_read_only


def observation_calendar_context(conn: sqlite3.Connection, day: date) -> tuple[date, bool]:
    """Resolve the required master date without consulting TradingView L2 rows."""
    calendar = conn.execute(
        "SELECT holiday_division FROM jquants_market_calendar WHERE day=?",
        (day.isoformat(),),
    ).fetchone()
    if calendar is None or calendar[0] not in {"0", "1", "2", "3"}:
        raise ValueError("Exact-date trading calendar is unavailable")
    business = calendar[0] in {"1", "2"}
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
            "SELECT COUNT(*) FROM jquants_market_calendar "
            "WHERE day BETWEEN ? AND ? AND holiday_division IN ('0', '1', '2', '3')",
            (basis.isoformat(), day.isoformat()),
        ).fetchone()[0]
        if calendar_days != (day - basis).days + 1:
            raise ValueError("Continuous trading calendar coverage is unavailable")
    return basis, business


def market_calendar_business_day(path: Path, day: date) -> bool | None:
    """Return whether ``day`` is a trading day, or None for absent or unknown categories.

    Calendar read failures raise rather than being treated as a closed market.
    An absent file, an unreadable store and
    an uncovered date remain distinct from a confirmed non-business day.
    """

    with closing(connect_read_only(path)) as connection:
        row = connection.execute(
            "SELECT holiday_division FROM jquants_market_calendar WHERE day = ?",
            (day.isoformat(),),
        ).fetchone()
    return None if row is None or row[0] not in {"0", "1", "2", "3"} else row[0] in {"1", "2"}


def read_price_calendar(connection: sqlite3.Connection, *, as_of: date) -> dict[date, bool]:
    """Share the saved calendar without applying another indicator's history requirement."""
    return {
        date.fromisoformat(row[0]): bool(row[1])
        for row in connection.execute(
            "SELECT day, holiday_division IN ('1','2') FROM jquants_market_calendar "
            "WHERE day<=? AND holiday_division IN ('0', '1', '2', '3') ORDER BY day",
            (as_of.isoformat(),),
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
        if division in {"1", "2"}:
            basis = day
        elif division not in {"0", "3"} or previous != day - timedelta(days=1):
            basis = None
        elif basis is not None and any(start <= basis <= day <= end for start, end in coverage):
            result[day] = basis
        previous = day
    return result
