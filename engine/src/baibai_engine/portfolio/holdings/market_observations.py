"""資本確認工程で市場quote・権利単位・発注営業日の読み取り入力を産む。

The read-only market runtime copy supplies both the previous business-day close
for a judgment instant and same-date holding-basis closes.

Portfolio/Position owns this quote and share-basis reader; Research consumes it
without making Portfolio/Position depend on Research. Storage observations owns
the physical schema version; these read-only queries use that contract directly.
An absent store or genuinely missing observation has no value. Schema mismatches
and SQL errors propagate; they are not ordinary missing market data.

For target-session planning, the resolved price is the raw/unadjusted ``close`` on the full-universe
daily bars' latest observed market-wide date eligible at that instant (today after
15:30 JST, otherwise prior). This reader does not certify full-market coverage. A
missing ticker row or NULL ``close`` on that exact date returns no price; an older
ticker row is never used as a substitute. A raw quote is an observation, not a
claim that its price/share basis matches another date. Consumers check that
relationship over the required period, separately from reading the price.
"""

from __future__ import annotations

import sqlite3
from contextlib import closing
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from math import isfinite
from pathlib import Path
from typing import Literal

from baibai_engine.machine_analysis.observations.prices.calendar import JQuantsMarketCalendarDay
from baibai_engine.machine_analysis.observations.prices.calendar_reader import (
    _read_calendar,
)
from baibai_engine.machine_analysis.observations.prices.price_basis import share_unit_factor
from baibai_engine.machine_analysis.observations.prices.price_models import JQuantsAdjustmentFactorEvent, JQuantsDailyBar
from baibai_engine.machine_analysis.observations.prices.price_reader import _read_ticker_bars
from baibai_engine.machine_analysis.observations.prices.sessions import latest_eligible_close_date
from baibai_engine.storage.sqlite.source_cache.read import connect_read_only
from baibai_engine.storage.sqlite.source_cache.schema import (
    SQLITE_SCHEMA_VERSION,
    SQLiteSchemaError,
    connect_current,
)


@dataclass(frozen=True, slots=True)
class UnadjustedCloseObservation:
    close_yen: float
    price_as_of: date


def read_unadjusted_close(
    *,
    sqlite_path: Path,
    ticker: str,
    at: datetime,
    connection: sqlite3.Connection | None = None,
) -> UnadjustedCloseObservation | None:
    """Return the ticker's raw close on the latest eligible observed market date.

    ``None`` means no raw close is available (missing store, missing coverage, or an
    adjusted-only row); the caller must not substitute an adjusted series.
    """
    latest_close_date = latest_eligible_close_date(at)
    conn = connection
    owns_connection = conn is None
    if conn is None:
        if not sqlite_path.exists():
            return None
        conn = connect_read_only(sqlite_path)
    try:
        if owns_connection:
            conn.execute("BEGIN")
        version_row = conn.execute("PRAGMA user_version").fetchone()
        if version_row is None or int(version_row[0]) != SQLITE_SCHEMA_VERSION:
            raise SQLiteSchemaError("market quote requires the current market schema")
        market_session_row = conn.execute(
            "SELECT MAX(traded_at) FROM jquants_daily_bars WHERE traded_at <= ?",
            (latest_close_date.isoformat(),),
        ).fetchone()
        if market_session_row is None or market_session_row[0] is None:
            return None
        price_as_of_text = str(market_session_row[0])
        # Require the selected ticker's row on the exact market-wide date. If
        # its raw close is missing, defer instead of silently using a stale bar.
        row = conn.execute(
            "SELECT traded_at, close FROM jquants_daily_bars WHERE ticker = ? AND traded_at = ?",
            (ticker, price_as_of_text),
        ).fetchone()
    finally:
        if owns_connection:
            conn.close()
    if row is None:
        return None
    traded_at, close = row
    if close is None:
        return None
    try:
        price_as_of = date.fromisoformat(str(traded_at))
        close_yen = float(close)
    except (TypeError, ValueError):
        return None
    if not isfinite(close_yen) or close_yen <= 0:
        # A non-positive close is corrupt; defer rather than sizing on it (a zero
        # close would otherwise divide by zero in lot sizing).
        return None
    return UnadjustedCloseObservation(
        close_yen=close_yen,
        price_as_of=price_as_of,
    )


def market_basis_is_confirmed(
    *,
    basis: Literal["price", "shares"],
    sqlite_path: Path,
    ticker: str,
    from_date: date,
    through_date: date,
    connection: sqlite3.Connection | None = None,
) -> bool:
    """Confirm unchanged price or share basis across the observed session range.

    The event window is from_date < event date <= through_date: the starting
    observation already uses its own session basis. See J-Quants daily-bar
    adjustment rules: https://jpx-jquants.com/ja/spec/eq-bars-daily/adj
    Price comparisons reject every non-unit price adjustment. Share quantities use
    ExRT: rights-issue price adjustments do not rescale existing shares. Missing
    rows/factors and unknown non-unit action kinds remain unresolved. Historical
    closes are irrelevant to this check; the current quote is checked separately.
    """
    if from_date > through_date:
        return False
    if from_date == through_date:
        return True
    conn = connection
    owns_connection = conn is None
    if conn is None:
        if not sqlite_path.exists():
            return False
        conn = connect_read_only(sqlite_path)
    try:
        if owns_connection:
            conn.execute("BEGIN")
        version_row = conn.execute("PRAGMA user_version").fetchone()
        if version_row is None or int(version_row[0]) != SQLITE_SCHEMA_VERSION:
            raise SQLiteSchemaError("market quote requires the current market schema")
        session_rows = conn.execute(
            "SELECT DISTINCT traded_at FROM jquants_daily_bars "
            "WHERE traded_at > ? AND traded_at <= ? ORDER BY traded_at",
            (from_date.isoformat(), through_date.isoformat()),
        ).fetchall()
        sessions = tuple(str(row[0]) for row in session_rows)
        if not sessions or sessions[-1] != through_date.isoformat():
            return False
        bar_rows = conn.execute(
            "SELECT traded_at, adjustment_factor, ex_right_type FROM jquants_daily_bars "
            "WHERE ticker = ? AND traded_at > ? AND traded_at <= ? ORDER BY traded_at",
            (ticker, from_date.isoformat(), through_date.isoformat()),
        ).fetchall()
        if tuple(str(row[0]) for row in bar_rows) != sessions:
            return False
    finally:
        if owns_connection:
            conn.close()
    for traded_at, adjustment_factor, ex_right_type in bar_rows:
        try:
            factor = float(adjustment_factor)
        except (TypeError, ValueError):
            return False
        if not isfinite(factor) or factor <= 0:
            return False
        if basis == "shares":
            event = JQuantsAdjustmentFactorEvent(
                ticker=ticker,
                traded_at=date.fromisoformat(str(traded_at)),
                adjustment_factor=factor,
                ex_right_type=ex_right_type,
            )
            if share_unit_factor(event) != 1.0:
                return False
        elif abs(factor - 1.0) > 1e-9:
            return False
    return True


def next_order_session(*, sqlite_path: Path, now: datetime) -> date | None:
    """Resolve the first unexpired session from the existing calendar, including holidays."""
    start = latest_eligible_close_date(now) + timedelta(days=1)
    if not sqlite_path.exists():
        return None
    with closing(connect_read_only(sqlite_path)) as connection:
        rows = connection.execute(
            "SELECT day,holiday_division IN ('1', '2') FROM jquants_market_calendar "
            "WHERE day>=? ORDER BY day LIMIT 14",
            (start.isoformat(),),
        ).fetchall()
    for i, (day, is_business_day) in enumerate(rows):
        expected = start + timedelta(days=i)
        if day != expected.isoformat():
            return None
        if is_business_day == 1:
            return expected
    return None


def read_portfolio_market_inputs(
    sqlite_path: Path,
    tickers: tuple[str, ...],
    start: date,
    end: date,
    *,
    connection: sqlite3.Connection | None = None,
) -> tuple[list[JQuantsMarketCalendarDay] | None, list[JQuantsDailyBar] | None]:
    """Select calendar and scoped prices from one SQLite read snapshot.

    Cash-only outcomes need a calendar, not artificial price coverage. A missing
    input remains unavailable; this does not create an archive or replay proof.
    A supplied connection belongs to the caller, including its read transaction.
    """
    owns_connection = connection is None
    conn = connect_current(sqlite_path) if owns_connection else connection
    if conn is None:
        return None, None
    try:
        if owns_connection:
            conn.execute("BEGIN")
        calendar = _read_calendar(conn, start, end)
        if calendar is None:
            return None, None
        return calendar, _read_ticker_bars(conn, tickers, start, end)
    finally:
        if owns_connection:
            conn.close()
