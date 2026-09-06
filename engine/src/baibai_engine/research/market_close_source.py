"""Provide raw/unadjusted closes for Research planning and holding valuation.

The read-only market runtime copy supplies both the previous business-day close
for a target session and same-date holding-basis closes.

Research authoring lives in ``thesis``, which the import DAG keeps off the
``baibai_engine.market`` package. This reader declares the expected market SQLite
schema version and queries full-universe ``jquants_daily_bars`` through read-only
SQL instead of importing the market package. Schema version mismatches and SQL
errors degrade to ``None``; a coupling test detects version drift in CI.

For target-session planning, the resolved price is the raw/unadjusted ``close`` on the full-universe
daily bars' latest market-wide session strictly before the target session. A
missing ticker row or NULL ``close`` on that exact date returns no price; an older
ticker row is never used as a substitute. An absent ``adjustment_factor`` or a
value other than 1 marks an unresolved corporate action so the caller defers
rather than quoting an unreconciled price.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import date
from math import isfinite
from pathlib import Path

# market SQLite の破壊的変更は version bump + rebuild で行われる。
# この reader は列名を境界越しに複製するため、想定 version を宣言し、実 store の
# PRAGMA user_version と突き合わせて drift を検出する。定数が market 側の
# SQLITE_SCHEMA_VERSION を追随することは coupling test が CI で保証し、version bump を
# 「silent degradation」ではなく赤い CI にする。実行時に不一致な store は no-coverage
# (None) へ degrade し、古い schema literal で誤読しない。
_EXPECTED_MARKET_SCHEMA_VERSION = 25


@dataclass(frozen=True, slots=True)
class UnadjustedCloseObservation:
    close_yen: float
    price_as_of: date
    adjustment_factor: float | None
    corporate_action_unresolved: bool


def read_prior_session_unadjusted_close(
    *,
    sqlite_path: Path,
    ticker: str,
    target_session: date,
    connection: sqlite3.Connection | None = None,
) -> UnadjustedCloseObservation | None:
    """Return the exact previous market-wide session's raw close before target.

    ``None`` means no raw close is available (missing store, missing coverage, or an
    adjusted-only row); the caller must not substitute an adjusted series.
    """
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


def read_holding_unadjusted_close_on_basis(
    *,
    sqlite_path: Path,
    ticker: str,
    ledger_price_observed_on: date,
    basis_as_of: date,
    connection: sqlite3.Connection | None = None,
) -> UnadjustedCloseObservation | None:
    """Return a holding's raw close only when its ledger-to-basis chain is complete.

    Revaluation is safe only when every full-universe market session from the
    ledger observation date through ``basis_as_of`` has an exact ticker bar, a
    positive raw close, and a confirmed adjustment factor of 1. ``None`` requires
    the caller to retain the canonical ledger market value and disclose fallback.
    """
    if ledger_price_observed_on > basis_as_of:
        return None
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
        session_rows = conn.execute(
            "SELECT DISTINCT traded_at FROM jquants_daily_bars "
            "WHERE traded_at BETWEEN ? AND ? ORDER BY traded_at",
            (ledger_price_observed_on.isoformat(), basis_as_of.isoformat()),
        ).fetchall()
        sessions = tuple(str(row[0]) for row in session_rows)
        if not sessions or sessions[-1] != basis_as_of.isoformat():
            return None
        bar_rows = conn.execute(
            "SELECT traded_at, close, adjustment_factor FROM jquants_daily_bars "
            "WHERE ticker = ? AND traded_at BETWEEN ? AND ? ORDER BY traded_at",
            (ticker, ledger_price_observed_on.isoformat(), basis_as_of.isoformat()),
        ).fetchall()
        if tuple(str(row[0]) for row in bar_rows) != sessions:
            return None
    except (sqlite3.Error, TypeError, ValueError):
        return None
    finally:
        if owns_connection:
            conn.close()
    basis_close: float | None = None
    for expected_session, row in zip(sessions, bar_rows, strict=True):
        traded_at, close, adjustment_factor = row
        if str(traded_at) != expected_session or close is None or adjustment_factor is None:
            return None
        try:
            close_yen = float(str(close))
            factor = float(str(adjustment_factor))
        except (TypeError, ValueError):
            return None
        if (
            not isfinite(close_yen)
            or close_yen <= 0
            or not isfinite(factor)
            or abs(factor - 1.0) > 1e-9
        ):
            return None
        if expected_session == basis_as_of.isoformat():
            basis_close = close_yen
    if basis_close is None:
        return None
    return UnadjustedCloseObservation(
        close_yen=basis_close,
        price_as_of=basis_as_of,
        adjustment_factor=1.0,
        corporate_action_unresolved=False,
    )
