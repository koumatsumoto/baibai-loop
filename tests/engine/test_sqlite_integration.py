"""End-to-end checks that the screening run and decision sync paths can be
served entirely from the SQLite cache, without falling back to any JSON
file or J-Quants / EDINET / JPX HTTP client.

The fixtures here build a minimal but complete SQLite database with the
exact rows each provider expects. The providers are wired with NO HTTP
client, so any unexpected fall-through raises and fails the test.
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from tests.helpers.screening_sqlite import add_source_coverage

from baibai_engine.foundation.time import JST
from baibai_engine.screening.cli import ProviderBundle, run_command
from baibai_engine.screening.config import ScreeningConfig
from baibai_engine.screening.providers import EDINETProvider, JPXProvider, JQuantsProvider
from baibai_engine.screening.run_store import ScreeningRunReader, run_store_path
from baibai_engine.screening.sqlite_cache import open_connection


def _populate_screening_fixture(sqlite_path: Path, asof: date) -> None:
    """Write the minimum SQLite rows the screening run needs to produce a
    valid YAML for `asof` without hitting any external provider.
    """
    sqlite_path.parent.mkdir(parents=True, exist_ok=True)
    conn = open_connection(sqlite_path)

    ticker = "130A"
    # Cover [asof - 2200, max(asof + 60, today + 5)]: the current screen reads
    # 1200 days, while split-safe normalized PER proves a 2200-day factor range.
    # Coverage is derived from actual rows, so the fixture must really hold them
    # rather than only claim coverage via source_coverage. The forward window extends
    # to today so the decision sync
    # path's `end = max(now, asof)` stays inside the imported window for any test
    # wall-clock — a fixed asof + 60 expires once the real clock passes that date.
    history_days_back = 2200
    forward_days = 60
    history_start = asof - timedelta(days=history_days_back)
    today = datetime.now(UTC).date()
    history_end = max(asof + timedelta(days=forward_days), today + timedelta(days=5))
    history_days = (history_end - history_start).days + 1
    bars_start = asof - timedelta(days=2200)
    fin_start = asof - timedelta(days=2200)

    # Master snapshot
    master_rows = [
        (
            asof.isoformat(),
            ticker,
            "Alpha",
            "Prime",
            "情報・通信業",
            1,
        ),
        *[
            (
                asof.isoformat(),
                f"{1000 + index:04d}",
                f"Company {index}",
                "Prime",
                "情報・通信業",
                1,
            )
            for index in range(2499)
        ],
    ]
    conn.executemany(
        "INSERT INTO jquants_master_snapshots("
        "snapshot_date, ticker, name, market, sector_33, is_common_stock"
        ") VALUES (?, ?, ?, ?, ?, ?)",
        master_rows,
    )
    add_source_coverage(
        conn,
        source="jquants_master_snapshots",
        coverage_key=f"get_eq_master:{asof.isoformat()}..{asof.isoformat()}",
        record_count=len(master_rows),
        min_date=asof.isoformat(),
        max_date=asof.isoformat(),
    )

    # Bars: 800 days of synthetic prices
    bars_rows = [
        (
            ticker,
            (history_start + timedelta(days=index)).isoformat(),
            None,
            None,
            None,
            float(100 + index),
            None,
            300_000_000.0,
            None,
            None,
            None,
            float(100 + index),
            None,
            None,
            None,
            None,
        )
        for index in range(history_days)
    ]
    conn.executemany(
        "INSERT INTO jquants_daily_bars("
        "ticker, traded_at, open, high, low, close, volume, turnover_value, "
        "adjustment_open, adjustment_high, adjustment_low, adjustment_close, "
        "adjustment_volume, adjustment_factor, upper_limit, lower_limit"
        ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        bars_rows,
    )
    # Mark the API request bracket the screening run will use; the
    # JQuantsProvider chunks at 31 days but the coverage check tolerates a
    # single import that contains the full window.
    add_source_coverage(
        conn,
        source="jquants_daily_bars",
        coverage_key=(
            f"records/_data/raw/screening/jquants/"
            f"get_eq_bars_daily_range-end_dt-{history_end.isoformat()}-"
            f"start_dt-{bars_start.isoformat()}.json"
        ),
        record_count=history_days,
        min_date=bars_start.isoformat(),
        max_date=history_end.isoformat(),
    )

    # Fin summaries: current rows plus three consecutive FY rows for normalized PER.
    fin_rows = [
        *[
            (
                ticker,
                (asof - timedelta(days=365 * years_ago - 30)).isoformat(),
                None,
                eps,
                None,
                None,
                None,
                None,
                None,
                None,
                "FY",
                date(asof.year - years_ago, 3, 31).isoformat(),
                None,
                None,
            )
            for years_ago, eps in ((3, 10.0), (2, 20.0), (1, 30.0))
        ],
        (
            ticker,
            (asof - timedelta(days=120)).isoformat(),
            20.0,
            15.0,
            120.0,
            400_000_000.0,
            1000.0,
            100.0,
            None,
            None,
            "Q4",
            None,
            None,
            None,
        ),
        (
            ticker,
            (asof - timedelta(days=15)).isoformat(),
            22.0,
            18.0,
            130.0,
            400_000_000.0,
            1100.0,
            110.0,
            None,
            None,
            "Q1",
            None,
            None,
            None,
        ),
    ]
    conn.executemany(
        "INSERT INTO jquants_fin_summaries("
        "ticker, disclosed_at, forecast_eps, eps_ttm, bps, shares_outstanding, "
        "sales, operating_profit, ordinary_profit, profit, "
        "fiscal_period, fiscal_year_end, period_start, period_end"
        ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        fin_rows,
    )
    add_source_coverage(
        conn,
        source="jquants_fin_summaries",
        coverage_key=(
            f"records/_data/raw/screening/jquants/"
            f"get_fin_summary_range-end_dt-{asof.isoformat()}-"
            f"start_dt-{fin_start.isoformat()}.json"
        ),
        record_count=len(fin_rows),
        min_date=fin_start.isoformat(),
        max_date=asof.isoformat(),
    )

    # Earnings calendar — at least one normalized row is required; an empty
    # covered payload is treated as incomplete so read-through can repair it.
    earnings_date = asof + timedelta(days=7)
    conn.execute(
        "INSERT INTO jquants_earnings_calendar(announcement_date, ticker) VALUES (?, ?)",
        (
            earnings_date.isoformat(),
            ticker,
        ),
    )
    add_source_coverage(
        conn,
        source="jpx_earnings_calendar",
        coverage_key="get_earnings_calendar_snapshot:current",
        record_count=1,
        min_date=earnings_date.isoformat(),
        max_date=earnings_date.isoformat(),
        fetched_at_utc=f"{asof.isoformat()}T00:00:00+09:00",
    )

    # Market calendar — populate the same forward window as bars so the
    # decision-register path's `[asof - 10, max(now, asof)]` range is always covered.
    calendar_rows = [
        (
            (history_start + timedelta(days=index)).isoformat(),
            1,
        )
        for index in range(history_days)
    ]
    conn.executemany(
        "INSERT INTO jquants_market_calendar(day, is_business_day) VALUES (?, ?)",
        calendar_rows,
    )
    add_source_coverage(
        conn,
        source="jquants_market_calendar",
        coverage_key="records/_data/raw/screening/jquants/get_mkt_calendar-from_yyyymmdd-X-to_yyyymmdd-Y.json",
        record_count=history_days,
        min_date=history_start.isoformat(),
        max_date=history_end.isoformat(),
    )

    # EDINET metrics for the same ticker
    conn.execute(
        "INSERT INTO edinet_metrics("
        "asof_date, ticker, sales_ttm, ocf_ttm, debt, cash, ebitda_ttm, "
        "consolidation_basis, ttm_quality_ev_ebitda, ttm_quality_p_s, ttm_quality_pcfr"
        ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            asof.isoformat(),
            ticker,
            1000.0,
            100.0,
            50.0,
            20.0,
            120.0,
            "consolidated",
            "exact",
            "approximated",
            "unavailable",
        ),
    )
    add_source_coverage(
        conn,
        source="edinet_metrics",
        coverage_key=f"records/_data/raw/screening/edinet/metrics/{asof.isoformat()}.json",
        record_count=1,
        min_date=asof.isoformat(),
        max_date=asof.isoformat(),
    )

    # JPX regulation flags (empty snapshot, asof imported)
    conn.executemany(
        "INSERT INTO jpx_regulation_sources(asof_date, source_name, fetched_at_utc) "
        "VALUES (?, ?, ?)",
        [
            (asof.isoformat(), "上場廃止警告", None),
            (
                asof.isoformat(),
                "取引停止",
                datetime(asof.year, asof.month, asof.day, tzinfo=UTC).isoformat(),
            ),
            (
                asof.isoformat(),
                "整理銘柄",
                datetime(asof.year, asof.month, asof.day, tzinfo=UTC).isoformat(),
            ),
            (
                asof.isoformat(),
                "特別注意銘柄",
                datetime(asof.year, asof.month, asof.day, tzinfo=UTC).isoformat(),
            ),
        ],
    )
    add_source_coverage(
        conn,
        source="jpx_regulation_flags",
        coverage_key=f"records/_data/raw/screening/jpx/regulations/{asof.isoformat()}.json",
        record_count=0,
        min_date=asof.isoformat(),
        max_date=asof.isoformat(),
    )

    conn.commit()
    conn.close()


class ScreeningRunOverSqliteTests(unittest.TestCase):
    def test_run_command_succeeds_using_only_sqlite_cache(self) -> None:
        asof = date(2026, 4, 24)
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = Path(tmpdir)
            import os

            cwd = Path.cwd()
            try:
                os.chdir(workspace)
                cache_dir = workspace / ".cache" / "screening"
                sqlite_dir = workspace / "data" / "screening"
                sqlite_path = sqlite_dir / "market.sqlite"
                _populate_screening_fixture(sqlite_path, asof)

                config = ScreeningConfig(
                    "token",
                    "key",
                    cache_dir=cache_dir,
                    sqlite_cache_dir=sqlite_dir,
                )
                providers = ProviderBundle(
                    jquants=JQuantsProvider(
                        config.jquants_api_key,
                        config.cache_dir,
                        sqlite_path=sqlite_path,
                    ),
                    edinet=EDINETProvider(
                        config.edinet_api_key,
                        config.cache_dir,
                        sqlite_path=sqlite_path,
                    ),
                    jpx=JPXProvider(
                        config.cache_dir,
                        sqlite_path=sqlite_path,
                    ),
                )

                exit_code = run_command(
                    asof,
                    config,
                    providers,
                    now=datetime(asof.year, asof.month, asof.day, 9, 0, tzinfo=JST),
                )

                publication = ScreeningRunReader(run_store_path()).latest_run()
                self.assertIn(exit_code, (0, 2))
                self.assertIsNotNone(publication, "screening run should be published")
                assert publication is not None
                payload = publication.payload
                self.assertEqual(payload["asof_date"], asof.isoformat())
                self.assertEqual(payload["run_id"], f"screening-{asof:%Y%m%d}")
            finally:
                os.chdir(cwd)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
