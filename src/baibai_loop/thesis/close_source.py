"""Read the previous business-day raw close directly from the L1 SQLite store.

Opportunity authoring lives in ``thesis``, which the import DAG keeps off the
``baibai_loop.market`` package. The market SQLite schema is a stable platform
contract (architecture 安定契約 contract 2: full-universe ``jquants_daily_bars``,
version-managed, AI may issue read-only SQL directly), so this reader opens the
store read-only and queries the documented columns, degrading to ``None`` on any
error instead of importing the market package.

The resolved price is always the raw/unadjusted ``close`` of the latest complete
business day strictly before the target session. A row whose ``close`` is NULL
(an adjusted-only series) is skipped so the caller never guesses a limit from an
adjusted price. A present ``adjustment_factor`` that is not 1 marks an unresolved
corporate action so the caller defers rather than quoting an unreconciled price.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path

# 21 暦日で直前営業日 (連休・年末年始を跨いでも直近の bar) を確実に含める。
_LOOKBACK_DAYS = 21

# market SQLite の破壊的変更は version bump + rebuild で行われる (architecture 安定契約 2)。
# この reader は列名を境界越しに複製するため、想定 version を宣言し、実 store の
# PRAGMA user_version と突き合わせて drift を検出する。定数が market 側の
# SQLITE_SCHEMA_VERSION を追随することは coupling test が CI で保証し、version bump を
# 「silent degradation」ではなく赤い CI にする。実行時に不一致な store は no-coverage
# (None) へ degrade し、古い schema literal で誤読しない。
_EXPECTED_MARKET_SCHEMA_VERSION = 12


@dataclass(frozen=True, slots=True)
class PreviousClose:
    close_yen: float
    price_as_of: date
    adjustment_factor: float | None
    corporate_action_unresolved: bool


def resolve_previous_business_day_close(
    *,
    sqlite_path: Path,
    ticker: str,
    target_session: date,
) -> PreviousClose | None:
    """Return the latest complete business-day raw close strictly before target.

    ``None`` means no raw close is available (missing store, missing coverage, or an
    adjusted-only row); the caller must not substitute an adjusted series.
    """
    if not sqlite_path.exists():
        return None
    window_start = (target_session - timedelta(days=_LOOKBACK_DAYS)).isoformat()
    window_end = (target_session - timedelta(days=1)).isoformat()
    try:
        conn = sqlite3.connect(f"file:{sqlite_path}?mode=ro", uri=True)
    except sqlite3.Error:
        return None
    try:
        version_row = conn.execute("PRAGMA user_version").fetchone()
        if version_row is None or int(version_row[0]) != _EXPECTED_MARKET_SCHEMA_VERSION:
            return None
        # Take the single latest business-day bar before target (no close filter): if
        # that day's raw close is missing we defer, rather than falling back to an
        # older raw close and quoting a stale price (D5: latest complete close missing).
        row = conn.execute(
            "SELECT traded_at, close, adjustment_factor FROM jquants_daily_bars "
            "WHERE ticker = ? AND traded_at BETWEEN ? AND ? "
            "ORDER BY traded_at DESC LIMIT 1",
            (ticker, window_start, window_end),
        ).fetchone()
    except sqlite3.Error:
        return None
    finally:
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
    if close_yen <= 0:
        # A non-positive close is corrupt; defer rather than sizing on it (a zero
        # close would otherwise divide by zero in lot sizing).
        return None
    corporate_action_unresolved = factor is not None and abs(factor - 1.0) > 1e-9
    return PreviousClose(
        close_yen=close_yen,
        price_as_of=price_as_of,
        adjustment_factor=factor,
        corporate_action_unresolved=corporate_action_unresolved,
    )
