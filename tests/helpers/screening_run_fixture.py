"""A frozen ``market.sqlite`` complete enough for one cache-only screening run.

The screen reads a dozen sources and refuses to fetch any of them in cache-only
mode, so a run that never touches the network needs all of them present at once.
This builder produces the smallest store where that holds: two instruments, a
continuous bar history long enough to satisfy the input window, and one coverage
row per source.

It exists so a comparison can hold every input constant and vary exactly one
thing. Nothing here is production methodology — the numbers only have to be
internally consistent enough for the screen to compute from them.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

from baibai_engine.screening import master_snapshot
from baibai_engine.screening.metrics import (
    BARS_INPUT_WINDOW_DAYS,
    NORMALIZED_EPS_HISTORY_WINDOW_DAYS,
)
from baibai_engine.screening.sqlite_cache import open_connection

from .screening_sqlite import add_source_coverage

ASOF = date(2026, 6, 30)
TICKERS = ("9001", "9002")
_HISTORY_DAYS = NORMALIZED_EPS_HISTORY_WINDOW_DAYS + 40
_MASTER_ROWS = master_snapshot.MIN_COMMON_STOCK_MASTER_ROWS
# Turnover has to clear the production liquidity floor, or the selection stage has
# nothing to rank and the comparison would only ever see an empty recommendation list.
_DAILY_VOLUME = 2_000_000.0
_FETCHED_AT = datetime(2026, 6, 30, 9, 0, tzinfo=UTC).isoformat()


def _trading_days(asof: date, days: int) -> list[date]:
    """Weekdays ending on ``asof``, which is what a continuous history looks like."""

    result: list[date] = []
    cursor = asof - timedelta(days=days)
    while cursor <= asof:
        if cursor.weekday() < 5:
            result.append(cursor)
        cursor += timedelta(days=1)
    return result


def build_screening_market_store(path: Path, *, asof: date = ASOF) -> Path:
    """Write a store one cache-only ``screening run`` can be executed against."""

    connection = open_connection(path)
    try:
        days = _trading_days(asof, _HISTORY_DAYS)
        _write_calendar(connection, days)
        _write_master(connection, asof)
        _write_bars(connection, days)
        _write_fin_summaries(connection, asof)
        _write_margin(connection, asof)
        _write_short_sale(connection, asof)
        _write_edinet(connection, asof)
        _write_jpx(connection, asof)
        connection.commit()
    finally:
        connection.close()
    return path


def _write_calendar(connection: sqlite3.Connection, days: list[date]) -> None:
    connection.executemany(
        "INSERT OR REPLACE INTO jquants_market_calendar(day, is_business_day) VALUES (?, ?)",
        [(day.isoformat(), 1) for day in days],
    )
    add_source_coverage(
        connection,
        source="jquants_market_calendar",
        coverage_key=f"{days[0].isoformat()}..{days[-1].isoformat()}",
        record_count=len(days),
        min_date=days[0].isoformat(),
        max_date=days[-1].isoformat(),
        fetched_at_utc=_FETCHED_AT,
    )


def _write_master(connection: sqlite3.Connection, asof: date) -> None:
    """The master must clear the production common-stock floor to be readable.

    Only ``TICKERS`` carry prices and financials; the rest exist so the snapshot is
    a plausible whole-exchange master and the reader's floor is met. They fall out
    of the universe for want of a bar history, which is the same way a real listing
    without price data behaves.
    """

    rows = [
        (asof.isoformat(), "9001", "キャッシュリッチ", "プライム", "サービス業", 1),
        (asof.isoformat(), "9002", "割高", "プライム", "サービス業", 1),
    ]
    rows.extend(
        (asof.isoformat(), f"{1000 + index:04d}", f"Filler {index}", "プライム", "サービス業", 1)
        for index in range(_MASTER_ROWS - len(rows))
    )
    connection.executemany(
        "INSERT OR REPLACE INTO jquants_master_snapshots("
        "snapshot_date, ticker, name, market, sector_33, is_common_stock"
        ") VALUES (?, ?, ?, ?, ?, ?)",
        rows,
    )
    add_source_coverage(
        connection,
        source=master_snapshot.MASTER_SOURCE,
        coverage_key=master_snapshot.master_coverage_key(asof),
        record_count=len(rows),
        min_date=asof.isoformat(),
        max_date=asof.isoformat(),
        fetched_at_utc=_FETCHED_AT,
    )


def _write_bars(connection: sqlite3.Connection, days: list[date]) -> None:
    rows = []
    for ticker, base in (("9001", 100.0), ("9002", 900.0)):
        for index, day in enumerate(days):
            close = base + (index % 7)
            rows.append(
                (
                    ticker,
                    day.isoformat(),
                    close,
                    close,
                    close,
                    close,
                    _DAILY_VOLUME,
                    close * _DAILY_VOLUME,
                    close,
                    1.0,
                )
            )
    connection.executemany(
        "INSERT OR REPLACE INTO jquants_daily_bars("
        "ticker, traded_at, open, high, low, close, volume, turnover_value, "
        "adjustment_close, adjustment_factor) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        rows,
    )
    add_source_coverage(
        connection,
        source="jquants_daily_bars",
        coverage_key=f"{days[0].isoformat()}..{days[-1].isoformat()}",
        record_count=len(rows),
        min_date=days[0].isoformat(),
        max_date=days[-1].isoformat(),
        fetched_at_utc=_FETCHED_AT,
    )


def _write_fin_summaries(connection: sqlite3.Connection, asof: date) -> None:
    columns = (
        "ticker, disclosed_at, forecast_eps, eps_ttm, bps, shares_outstanding, "
        "sales, cfo, cash_eq, total_assets, equity, operating_profit, ordinary_profit, "
        "profit, fiscal_period, fiscal_year_end, period_start, period_end, "
        "dps_actual_annual, dps_forecast_annual, treasury_shares, equity_to_asset_ratio"
    )
    rows = []
    for offset in range(5):
        year = asof.year - offset
        disclosed = date(year, 5, 10)
        rows.extend(
            (
                (
                    "9001",
                    disclosed.isoformat(),
                    11.0,
                    10.0,
                    200.0,
                    1e8,
                    5e9,
                    1e9,
                    4e9,
                    3e10,
                    2e10,
                    5e8,
                    5e8,
                    1e9,
                    "FY",
                    f"{year}-03-31",
                    f"{year - 1}-04-01",
                    f"{year}-03-31",
                    4.0,
                    4.5,
                    0.0,
                    2e10 / 3e10,
                ),
                (
                    "9002",
                    disclosed.isoformat(),
                    1.0,
                    1.0,
                    10.0,
                    1e8,
                    5e9,
                    1e8,
                    1e8,
                    2e10,
                    5e9,
                    5e8,
                    5e8,
                    1e8,
                    "FY",
                    f"{year}-03-31",
                    f"{year - 1}-04-01",
                    f"{year}-03-31",
                    None,
                    None,
                    0.0,
                    5e9 / 2e10,
                ),
            )
        )
    connection.executemany(
        f"INSERT OR REPLACE INTO jquants_fin_summaries({columns}) "  # nosec B608
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        rows,
    )
    start = asof - timedelta(days=NORMALIZED_EPS_HISTORY_WINDOW_DAYS + 40)
    add_source_coverage(
        connection,
        source="jquants_fin_summaries",
        coverage_key=f"{start.isoformat()}..{asof.isoformat()}",
        record_count=len(rows),
        min_date=start.isoformat(),
        max_date=asof.isoformat(),
        fetched_at_utc=_FETCHED_AT,
    )


def _write_margin(connection: sqlite3.Connection, asof: date) -> None:
    week_end = asof - timedelta(days=asof.weekday() + 3)
    weeks = [week_end - timedelta(days=7 * index) for index in range(30)]
    connection.executemany(
        "INSERT OR REPLACE INTO jquants_weekly_margin("
        "ticker, week_end, long_vol, short_vol, long_std_vol, long_neg_vol, "
        "short_std_vol, short_neg_vol, issue_type) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [
            (ticker, day.isoformat(), 1000.0, 250.0, 800.0, 200.0, 200.0, 50.0, "2")
            for ticker in TICKERS
            for day in weeks
        ],
    )
    for day in weeks:
        add_source_coverage(
            connection,
            source="jquants_weekly_margin",
            coverage_key=day.isoformat(),
            record_count=len(TICKERS),
            min_date=day.isoformat(),
            max_date=day.isoformat(),
            fetched_at_utc=_FETCHED_AT,
        )
    start = asof - timedelta(days=400)
    add_source_coverage(
        connection,
        source="jquants_margin_alerts",
        coverage_key=f"{start.isoformat()}..{asof.isoformat()}",
        record_count=0,
        min_date=start.isoformat(),
        max_date=asof.isoformat(),
        fetched_at_utc=_FETCHED_AT,
    )


def _write_short_sale(connection: sqlite3.Connection, asof: date) -> None:
    disclosed = asof - timedelta(days=3)
    connection.executemany(
        "INSERT OR REPLACE INTO jquants_short_sale_reports("
        "disclosed_at, source_ordinal, calculated_at, ticker, short_seller_name, "
        "discretionary_investment_contractor_name, investment_fund_name, short_ratio, "
        "short_shares, short_trading_units, is_cancellation"
        ") VALUES (?, ?, ?, ?, ?, '', '', ?, ?, ?, 0)",
        [
            (
                disclosed.isoformat(),
                index,
                (disclosed - timedelta(days=1)).isoformat(),
                ticker,
                f"Fund {index}",
                0.006,
                600,
                6,
            )
            for index, ticker in enumerate(TICKERS)
        ],
    )
    start = asof - timedelta(days=400)
    add_source_coverage(
        connection,
        source="jquants_short_sale_reports",
        coverage_key=f"{start.isoformat()}..{asof.isoformat()}",
        record_count=len(TICKERS),
        min_date=start.isoformat(),
        max_date=asof.isoformat(),
        fetched_at_utc=_FETCHED_AT,
    )


def _write_edinet(connection: sqlite3.Connection, asof: date) -> None:
    connection.executemany(
        "INSERT OR REPLACE INTO edinet_metrics("
        "asof_date, ticker, debt, cash, net_cash, investment_securities, "
        "total_assets, consolidation_basis, failure_reasons, extractor_revision"
        ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [
            (asof.isoformat(), "9001", 1e9, 4e9, 3e9, 2e9, 3e10, "consolidated", "[]", "a" * 64),
            (asof.isoformat(), "9002", 1e9, 1e8, -9e8, 0.0, 2e10, "consolidated", "[]", "a" * 64),
        ],
    )
    add_source_coverage(
        connection,
        source="edinet_metrics",
        coverage_key=asof.isoformat(),
        record_count=2,
        min_date=asof.isoformat(),
        max_date=asof.isoformat(),
        fetched_at_utc=_FETCHED_AT,
    )
    start = asof - timedelta(days=400)
    for source in ("edinet_documents", "edinet_document_lists"):
        add_source_coverage(
            connection,
            source=source,
            coverage_key=f"{start.isoformat()}..{asof.isoformat()}",
            record_count=0,
            min_date=start.isoformat(),
            max_date=asof.isoformat(),
            fetched_at_utc=_FETCHED_AT,
        )


def _write_jpx(connection: sqlite3.Connection, asof: date) -> None:
    connection.executemany(
        "INSERT OR REPLACE INTO jpx_regulation_sources(asof_date, source_name, fetched_at_utc) "
        "VALUES (?, ?, ?)",
        [
            (asof.isoformat(), name, _FETCHED_AT)
            for name in ("特別注意銘柄", "整理銘柄", "取引停止", "上場廃止警告")
        ],
    )
    add_source_coverage(
        connection,
        source="jpx_regulation_flags",
        coverage_key=asof.isoformat(),
        record_count=0,
        min_date=asof.isoformat(),
        max_date=asof.isoformat(),
        fetched_at_utc=_FETCHED_AT,
    )
    announcement = (asof + timedelta(days=40)).isoformat()
    connection.executemany(
        "INSERT OR REPLACE INTO jquants_earnings_calendar(announcement_date, ticker) VALUES (?, ?)",
        [(announcement, ticker) for ticker in TICKERS],
    )
    add_source_coverage(
        connection,
        source="jpx_earnings_calendar",
        coverage_key="get_earnings_calendar_snapshot:current",
        record_count=len(TICKERS),
        min_date=announcement,
        max_date=announcement,
        fetched_at_utc=_FETCHED_AT,
    )


__all__ = ["ASOF", "BARS_INPUT_WINDOW_DAYS", "TICKERS", "build_screening_market_store"]
