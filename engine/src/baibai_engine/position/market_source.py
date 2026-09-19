"""資本確認工程で市場quote・権利単位・発注営業日の読み取り入力を産む。

The read-only market runtime copy supplies both the previous business-day close
for a judgment instant and same-date holding-basis closes.

Research authoring lives in ``thesis``, which the import DAG keeps off the
``baibai_engine.market`` package. This reader declares the expected market SQLite
schema version and queries full-universe ``jquants_daily_bars`` through read-only
SQL instead of importing the market package. Schema version mismatches and SQL
errors degrade to ``None``; a coupling test detects version drift in CI.

For target-session planning, the resolved price is the raw/unadjusted ``close`` on the full-universe
daily bars' latest complete market-wide session (today after 15:30 JST, otherwise prior). A
missing ticker row or NULL ``close`` on that exact date returns no price; an older
ticker row is never used as a substitute. An absent ``adjustment_factor`` or a
value other than 1 marks an unresolved corporate action so the caller defers
rather than quoting an unreconciled price.
"""

from __future__ import annotations

import sqlite3
from contextlib import closing
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from math import isfinite
from pathlib import Path

from baibai_engine.foundation.time import JST

# market SQLite の破壊的変更は version bump + rebuild で行われる。
# この reader は列名を境界越しに複製するため、想定 version を宣言し、実 store の
# PRAGMA user_version と突き合わせて drift を検出する。定数が market 側の
# SQLITE_SCHEMA_VERSION を追随することは coupling test が CI で保証し、version bump を
# 「silent degradation」ではなく赤い CI にする。実行時に不一致な store は no-coverage
# (None) へ degrade し、古い schema literal で誤読しない。
_EXPECTED_MARKET_SCHEMA_VERSION = 26


@dataclass(frozen=True, slots=True)
class UnadjustedCloseObservation:
    close_yen: float
    price_as_of: date
    adjustment_factor: float | None
    corporate_action_unresolved: bool


def read_unadjusted_close(
    *,
    sqlite_path: Path,
    ticker: str,
    at: datetime,
    connection: sqlite3.Connection | None = None,
) -> UnadjustedCloseObservation | None:
    """Return the latest complete market-wide session available at the given instant.

    ``None`` means no raw close is available (missing store, missing coverage, or an
    adjusted-only row); the caller must not substitute an adjusted series.
    """
    if at.tzinfo is None or at.utcoffset() is None:
        raise ValueError("quote instant requires a timezone")
    local_at = at.astimezone(JST)
    target_session = local_at.date() + timedelta(days=local_at.time() >= time(15, 30))
    conn = connection
    owns_connection = conn is None
    if conn is None:
        if not sqlite_path.exists():
            return None
        try:
            conn = sqlite3.connect(f"file:{sqlite_path}?mode=ro", uri=True)
        except sqlite3.Error:
            return None
    try:
        version_row = conn.execute("PRAGMA user_version").fetchone()
        if version_row is None or int(version_row[0]) != _EXPECTED_MARKET_SCHEMA_VERSION:
            return None
        market_session_row = conn.execute(
            "SELECT MAX(traded_at) FROM jquants_daily_bars WHERE traded_at < ?",
            (target_session.isoformat(),),
        ).fetchone()
        if market_session_row is None or market_session_row[0] is None:
            return None
        price_as_of_text = str(market_session_row[0])
        # Require the selected ticker's row on the exact market-wide date. If
        # its raw close is missing, defer instead of silently using a stale bar.
        row = conn.execute(
            "SELECT traded_at, close, adjustment_factor FROM jquants_daily_bars "
            "WHERE ticker = ? AND traded_at = ?",
            (ticker, price_as_of_text),
        ).fetchone()
    except sqlite3.Error:
        return None
    finally:
        if owns_connection:
            conn.close()
    if row is None:
        return None
    traded_at, close, adjustment_factor = row
    if close is None:
        return None
    try:
        price_as_of = date.fromisoformat(str(traded_at))
        close_yen = float(close)
        factor = float(adjustment_factor) if adjustment_factor is not None else None
    except (TypeError, ValueError):
        return None
    if not isfinite(close_yen) or close_yen <= 0:
        # A non-positive close is corrupt; defer rather than sizing on it (a zero
        # close would otherwise divide by zero in lot sizing).
        return None
    corporate_action_unresolved = factor is None or not isfinite(factor) or abs(factor - 1.0) > 1e-9
    return UnadjustedCloseObservation(
        close_yen=close_yen,
        price_as_of=price_as_of,
        adjustment_factor=factor,
        corporate_action_unresolved=corporate_action_unresolved,
    )


def quantity_basis_is_confirmed(
    *,
    sqlite_path: Path,
    ticker: str,
    from_date: date,
    through_date: date,
    connection: sqlite3.Connection | None = None,
) -> bool:
    """Confirm unchanged share units across observed market sessions, independent of closes.

    Missing ticker rows/factors or any rights change remain unresolved. A historical
    NULL close is irrelevant to quantity identity; current quote is checked separately.
    """
    if from_date > through_date:
        return False
    conn = connection
    owns_connection = conn is None
    if conn is None:
        if not sqlite_path.exists():
            return False
        try:
            conn = sqlite3.connect(f"file:{sqlite_path}?mode=ro", uri=True)
        except sqlite3.Error:
            return False
    try:
        version_row = conn.execute("PRAGMA user_version").fetchone()
        if version_row is None or int(version_row[0]) != _EXPECTED_MARKET_SCHEMA_VERSION:
            return False
        session_rows = conn.execute(
            "SELECT DISTINCT traded_at FROM jquants_daily_bars "
            "WHERE traded_at BETWEEN ? AND ? ORDER BY traded_at",
            (from_date.isoformat(), through_date.isoformat()),
        ).fetchall()
        sessions = tuple(str(row[0]) for row in session_rows)
        if not sessions or sessions[-1] != through_date.isoformat():
            return False
        bar_rows = conn.execute(
            "SELECT traded_at, adjustment_factor FROM jquants_daily_bars "
            "WHERE ticker = ? AND traded_at BETWEEN ? AND ? ORDER BY traded_at",
            (ticker, from_date.isoformat(), through_date.isoformat()),
        ).fetchall()
        if tuple(str(row[0]) for row in bar_rows) != sessions:
            return False
    except (sqlite3.Error, TypeError, ValueError):
        return False
    finally:
        if owns_connection:
            conn.close()
    for _, adjustment_factor in bar_rows:
        try:
            factor = float(adjustment_factor)
        except (TypeError, ValueError):
            return False
        if not isfinite(factor) or abs(factor - 1.0) > 1e-9:
            return False
    return True


def next_order_session(*, sqlite_path: Path, now: datetime) -> date | None:
    """Resolve the first unexpired session from the existing calendar, including holidays."""
    local = now.astimezone(JST)
    start = local.date() + timedelta(days=local.time() >= time(15, 30))
    try:
        with closing(sqlite3.connect(f"file:{sqlite_path}?mode=ro", uri=True)) as connection:
            rows = connection.execute(
                "SELECT day,is_business_day FROM jquants_market_calendar "
                "WHERE day>=? ORDER BY day LIMIT 14",
                (start.isoformat(),),
            ).fetchall()
    except sqlite3.Error:
        return None
    for i, (day, is_business_day) in enumerate(rows):
        expected = start + timedelta(days=i)
        if day != expected.isoformat():
            return None
        if is_business_day == 1:
            return expected
    return None
