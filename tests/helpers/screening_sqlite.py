"""Test helpers for the screening market.sqlite cache.

Common fixture builders that previously appeared verbatim across multiple
test modules. Keep these strictly minimal — only the duplications that exist
in real test code.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

from baibai_engine.screening.sqlite_cache import open_connection


def make_master_records(
    asof: date,
    *,
    count: int = 2500,
    excluded_count: int = 0,
) -> list[dict[str, str]]:
    """Return a complete official-key master response for offline tests."""
    records = [
        {
            "Date": asof.isoformat(),
            "Code": f"{1000 + index:04d}0",
            "CoName": f"Company {index}",
            "MktNm": "Prime",
            "S33Nm": "情報・通信業",
        }
        for index in range(count)
    ]
    records.extend(
        {
            "Date": asof.isoformat(),
            "Code": f"{9000 + index:04d}1",
            "CoName": f"Excluded {index}",
            "MktNm": "Prime",
            "S33Nm": "情報・通信業",
        }
        for index in range(excluded_count)
    )
    return records


def add_source_coverage(
    conn: sqlite3.Connection,
    *,
    source: str,
    coverage_key: str,
    record_count: int = 1,
    min_date: str | None = None,
    max_date: str | None = None,
    status: str = "ok",
    error: str | None = None,
    fetched_at_utc: str | None = None,
) -> None:
    """Insert one ``source_coverage`` row, mirroring the production INSERT shape."""
    fetched_at = fetched_at_utc or datetime.now(UTC).isoformat()
    conn.execute(
        "INSERT OR REPLACE INTO source_coverage("
        "source, coverage_key, coverage_start, coverage_end, fetched_at_utc, record_count, "
        "status, error"
        ") VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (source, coverage_key, min_date, max_date, fetched_at, record_count, status, error),
    )


def insert_daily_bars_from_closes(
    sqlite_path: Path,
    ticker: str,
    closes: list[float],
    *,
    end_date: date,
    turnover_value: float | None = None,
) -> None:
    """Insert ``len(closes)`` consecutive daily bars ending on ``end_date``.

    ``adjustment_close`` mirrors ``close`` (the test fixtures are unadjusted by
    design). ``turnover_value`` is included only when supplied, so callers that
    do not need a turnover column do not get a non-NULL row.
    """
    start = end_date - timedelta(days=len(closes) - 1)
    if turnover_value is None:
        columns = "ticker, traded_at, close, adjustment_close"
        rows: list[tuple[object, ...]] = [
            (ticker, (start + timedelta(days=index)).isoformat(), close, close)
            for index, close in enumerate(closes)
        ]
        placeholders = "?, ?, ?, ?"
    else:
        columns = "ticker, traded_at, close, adjustment_close, turnover_value"
        rows = [
            (ticker, (start + timedelta(days=index)).isoformat(), close, close, turnover_value)
            for index, close in enumerate(closes)
        ]
        placeholders = "?, ?, ?, ?, ?"
    conn = open_connection(sqlite_path)
    try:
        conn.executemany(
            f"INSERT OR REPLACE INTO jquants_daily_bars({columns}) VALUES ({placeholders})",
            rows,
        )
        conn.commit()
    finally:
        conn.close()


CALIBRATION_FIXTURE_ASOF = date(2026, 6, 30)


def build_calibration_fixture_sqlite(sqlite_path: Path) -> None:
    """Two priced, disclosed names — the smallest market store a panel build accepts."""

    ASOF = CALIBRATION_FIXTURE_ASOF

    conn = open_connection(sqlite_path)
    try:
        conn.executemany(
            "INSERT OR REPLACE INTO jquants_master_snapshots("
            "snapshot_date, ticker, name, market, sector_33, is_common_stock"
            ") VALUES (?, ?, ?, ?, ?, ?)",
            [
                ("2026-06-01", "9001", "キャッシュリッチ", "プライム", "サービス業", 1),
                ("2026-06-01", "9002", "割高", "プライム", "サービス業", 1),
            ],
        )
        add_source_coverage(
            conn,
            source="jquants_master_snapshots",
            coverage_key="latest",
            record_count=2,
            min_date="2026-06-01",
            max_date="2026-06-01",
        )
        fin_columns = (
            "ticker, disclosed_at, forecast_eps, eps_ttm, bps, shares_outstanding, "
            "sales, cfo, cash_eq, total_assets, equity, operating_profit, ordinary_profit, "
            "profit, fiscal_period, fiscal_year_end, period_start, period_end, "
            "dps_actual_annual, dps_forecast_annual, treasury_shares, equity_to_asset_ratio"
        )
        conn.executemany(
            f"INSERT OR REPLACE INTO jquants_fin_summaries({fin_columns}) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (
                    "9001",
                    "2026-05-10",
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
                    # 報告純利益は 1 株当たり当期純利益 x 自己株控除後株数と一致する。
                    # 自己資本も `bps x 自己株控除後株数 == 総資産 x 自己資本比率`
                    # (200 x 1e8 == 3e10 x 2/3) を満たす。倍率はこの行から出るので、
                    # 行の中で両方の恒等式が成り立っている必要がある。
                    1e9,
                    "FY",
                    "2026-03-31",
                    "2025-04-01",
                    "2026-03-31",
                    4.0,
                    4.5,
                    0.0,
                    2e10 / 3e10,
                ),
                (
                    "9002",
                    "2026-05-10",
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
                    "2026-03-31",
                    "2025-04-01",
                    "2026-03-31",
                    None,
                    None,
                    0.0,
                    5e9 / 2e10,
                ),
            ],
        )
        add_source_coverage(
            conn,
            source="jquants_fin_summaries",
            coverage_key="2026",
            record_count=2,
            min_date="2026-05-10",
            max_date="2026-06-30",
        )
        conn.execute(
            # 総資産と基準は、EDINET の貸借対照表が短信と同じ実体を指すことを示す事実として
            # 持つ。短信の総資産 (3e10) と揃わない行は EDINET 由来の値を出さない。
            "INSERT INTO edinet_metrics("
            "asof_date, ticker, debt, cash, net_cash, investment_securities, "
            "total_assets, consolidation_basis, failure_reasons, extractor_revision"
            ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                ASOF.isoformat(),
                "9001",
                1e9,
                4e9,
                3e9,
                2e9,
                3e10,
                "consolidated",
                "[]",
                "a" * 64,
            ),
        )
        add_source_coverage(
            conn,
            source="edinet_metrics",
            coverage_key=ASOF.isoformat(),
            record_count=1,
            min_date=ASOF.isoformat(),
            max_date=ASOF.isoformat(),
        )
        conn.commit()
    finally:
        conn.close()
    for ticker in ("9001", "9002"):
        insert_daily_bars_from_closes(
            sqlite_path,
            ticker,
            [100.0] * 200,
            end_date=ASOF,
            turnover_value=2e8,
        )
