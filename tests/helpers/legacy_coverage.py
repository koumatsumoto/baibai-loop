"""旧cache coverage検証用の100社合成入力。現行候補政策の充足条件ではない。"""

from __future__ import annotations

import shutil
import sqlite3
import tempfile
from collections.abc import Iterator
from contextlib import closing, contextmanager
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from baibai_engine.machine_analysis.historical_evaluation.legacy_screening.sqlite_coverage import (
    verify_screening_sqlite_coverage,
)
from baibai_engine.storage.sqlite.source_cache.schema import open_connection
from tests.helpers.master import insert_master_rows
from tests.helpers.screening_sqlite import add_source_coverage as _add_source_coverage
from tests.helpers.screening_sqlite import insert_financial_record


def _verify_screening_sqlite_coverage(*args, **kwargs):
    with patch(
        "baibai_engine.machine_analysis.observations.stocks.master_snapshot.MIN_COMMON_STOCK_MASTER_ROWS",
        100,
    ):
        return verify_screening_sqlite_coverage(*args, **kwargs)


_COMMON_COVERAGE_ASOF = date(2026, 5, 8)
_COMPLETE_COVERAGE_TEMPLATE_DIR: tempfile.TemporaryDirectory[str] | None = None
_COMPLETE_COVERAGE_TEMPLATE_PATH: Path | None = None


def _seed_complete_coverage(conn: sqlite3.Connection, asof: date) -> None:
    bars_start = asof - timedelta(days=2200)
    margin_week = (asof - timedelta(days=4)).isoformat()
    conn.execute(
        "INSERT OR REPLACE INTO jquants_weekly_margin("
        "week_end, ticker, long_vol, short_vol, long_std_vol, long_neg_vol, "
        "short_std_vol, short_neg_vol, issue_type) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (margin_week, "1301", 1.0, 1.0, 1.0, 0.0, 1.0, 0.0, "2"),
    )
    _add_source_coverage(
        conn,
        source="jquants_weekly_margin",
        coverage_key=f"get_mkt_margin_interest:{margin_week}..{margin_week}",
        record_count=1,
        min_date=margin_week,
        max_date=margin_week,
    )
    fin_start = asof - timedelta(days=2200)
    tickers = tuple(f"{1301 + index:04d}" for index in range(100))
    insert_master_rows(
        conn,
        [
            (asof.isoformat(), ticker, f"Name {ticker}", "Prime", "水産・農林業", "011")
            for ticker in tickers
        ],
    )
    _add_source_coverage(
        conn,
        source="jquants_master_snapshots",
        coverage_key=f"get_eq_master:{asof.isoformat()}..{asof.isoformat()}",
        record_count=len(tickers),
        min_date=asof.isoformat(),
        max_date=asof.isoformat(),
    )
    bar_rows: list[tuple[str, str, float, float]] = []
    current = bars_start
    while current <= asof:
        if current.weekday() < 5:
            bar_rows.extend(
                (ticker, current.isoformat(), 1000.0, 200_000_000.0) for ticker in tickers
            )
        current += timedelta(days=1)
    conn.executemany(
        "INSERT OR REPLACE INTO jquants_daily_bars(ticker, traded_at, close, turnover_value) "
        "VALUES (?, ?, ?, ?)",
        bar_rows,
    )
    _add_source_coverage(
        conn,
        source="jquants_daily_bars",
        coverage_key=f"get_eq_bars_daily_range:{bars_start.isoformat()}..{asof.isoformat()}",
        record_count=len(bar_rows),
        min_date=bars_start.isoformat(),
        max_date=asof.isoformat(),
    )
    for ticker in tickers:
        insert_financial_record(
            conn,
            {
                "Code": str(ticker) + "0",
                "DiscDate": asof.isoformat(),
                "EPS": 100.0,
                "ShOutFY": 10000000.0,
                "TrShFY": 1000000.0,
                "EqAR": 0.5,
                "DiscNo": asof.isoformat(),
            },
        )
    _add_source_coverage(
        conn,
        source="jquants_fin_summaries",
        coverage_key=f"get_fin_summary_range:{fin_start.isoformat()}..{asof.isoformat()}",
        record_count=len(tickers),
        min_date=fin_start.isoformat(),
        max_date=asof.isoformat(),
    )
    conn.execute(
        "INSERT OR REPLACE INTO jquants_market_calendar(day, holiday_division) VALUES (?, ?)",
        (asof.isoformat(), 1),
    )
    _add_source_coverage(
        conn,
        source="jquants_market_calendar",
        coverage_key=(f"get_mkt_calendar:{asof.isoformat()}..{asof.isoformat()}"),
        record_count=1,
        min_date=asof.isoformat(),
        max_date=asof.isoformat(),
    )
    _add_source_coverage(
        conn,
        source="jpx_regulation_flags",
        coverage_key=f"jpx_regulations:{asof.isoformat()}",
        record_count=0,
        min_date=asof.isoformat(),
        max_date=asof.isoformat(),
    )
    conn.execute(
        "INSERT OR REPLACE INTO jpx_regulation_sources(asof_date, source_name, fetched_at_utc) "
        "VALUES (?, ?, ?)",
        (
            asof.isoformat(),
            "test-source",
            datetime.combine(asof, datetime.min.time(), UTC).isoformat(),
        ),
    )
    _add_source_coverage(
        conn,
        source="edinet_metrics",
        coverage_key=asof.isoformat(),
        record_count=1,
        min_date=asof.isoformat(),
        max_date=asof.isoformat(),
    )
    conn.execute(
        "INSERT OR REPLACE INTO edinet_metrics(asof_date, ticker, sales_ttm) VALUES (?, ?, ?)",
        (asof.isoformat(), "1301", 1_000_000.0),
    )


def _complete_coverage_template_path() -> Path:
    global _COMPLETE_COVERAGE_TEMPLATE_DIR, _COMPLETE_COVERAGE_TEMPLATE_PATH
    if _COMPLETE_COVERAGE_TEMPLATE_PATH is None:
        template_dir = tempfile.TemporaryDirectory()
        template_path = Path(template_dir.name) / "market.sqlite"
        with closing(open_connection(template_path)) as conn, conn:
            _seed_complete_coverage(conn, _COMMON_COVERAGE_ASOF)
            conn.commit()
        _COMPLETE_COVERAGE_TEMPLATE_DIR = template_dir
        _COMPLETE_COVERAGE_TEMPLATE_PATH = template_path
    return _COMPLETE_COVERAGE_TEMPLATE_PATH


@contextmanager
def _complete_coverage_database() -> Iterator[Path]:
    with tempfile.TemporaryDirectory() as tmp:
        sqlite_path = Path(tmp) / "market.sqlite"
        shutil.copyfile(_complete_coverage_template_path(), sqlite_path)
        yield sqlite_path


def cleanup_coverage_template() -> None:
    global _COMPLETE_COVERAGE_TEMPLATE_DIR, _COMPLETE_COVERAGE_TEMPLATE_PATH
    if _COMPLETE_COVERAGE_TEMPLATE_DIR is not None:
        _COMPLETE_COVERAGE_TEMPLATE_DIR.cleanup()
        _COMPLETE_COVERAGE_TEMPLATE_DIR = None
        _COMPLETE_COVERAGE_TEMPLATE_PATH = None
