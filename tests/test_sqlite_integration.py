"""End-to-end checks that the screening run and ledger sync paths can be
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

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from baibai_loop.foundation.time import JST
from baibai_loop.foundation.yaml_io import safe_load
from baibai_loop.screening.cli import ProviderBundle, run_command
from baibai_loop.screening.config import ScreeningConfig
from baibai_loop.screening.providers import EDINETProvider, JPXProvider, JQuantsProvider
from baibai_loop.screening.render import build_output_path
from baibai_loop.screening.sqlite_cache import open_connection
from tests.helpers.screening_sqlite import add_source_coverage


def _populate_screening_fixture(sqlite_path: Path, asof: date) -> None:
    """Write the minimum SQLite rows the screening run needs to produce a
    valid YAML for `asof` without hitting any external provider.
    """
    sqlite_path.parent.mkdir(parents=True, exist_ok=True)
    conn = open_connection(sqlite_path)

    ticker = "130A"
    # Cover the full [asof - 1200, asof + 60] window: `screening run` requires
    # 1200 calendar days of daily bars, and coverage is now derived from the
    # actual rows, so the fixture must really hold them (not just claim coverage
    # via source_coverage). The +60 keeps the ledger sync path's
    # `end = max(now, asof)` inside the imported window for any test wall-clock.
    history_days_back = 1200
    forward_days = 60
    history_start = asof - timedelta(days=history_days_back)
    history_end = asof + timedelta(days=forward_days)
    history_days = history_days_back + forward_days + 1
    bars_start = asof - timedelta(days=1200)
    fin_start = asof - timedelta(days=730)

    # Master snapshot
    conn.execute(
        "INSERT INTO jquants_master_snapshots("
        "snapshot_date, ticker, name, market, sector_33, is_common_stock"
        ") VALUES (?, ?, ?, ?, ?, ?)",
        (
            asof.isoformat(),
            ticker,
            "Alpha",
            "Prime",
            "情報・通信業",
            1,
        ),
    )
    add_source_coverage(
        conn,
        source="jquants_master_snapshots",
        coverage_key="records/_data/raw/screening/jquants/get_eq_master.json",
        record_count=1,
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

    # Fin summaries: two disclosures spanning a year
    fin_rows = [
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
        record_count=2,
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
        source="jquants_earnings_calendar",
        coverage_key="records/_data/raw/screening/jquants/get_eq_earnings_cal.json",
        record_count=1,
        min_date=asof.isoformat(),
        max_date=(asof + timedelta(days=90)).isoformat(),
    )

    # Market calendar — populate the same forward window as bars so the
    # ledger path's `[asof - 10, max(now, asof)]` range is always covered.
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

                output_path = build_output_path(asof)
                self.assertIn(exit_code, (0, 2))
                self.assertTrue(output_path.exists(), "screening YAML should be written")
                payload = safe_load(output_path.read_text(encoding="utf-8"))
                self.assertEqual(payload["asof_date"], asof.isoformat())
                self.assertEqual(payload["run_id"], f"screening-{asof:%Y%m%d}")
            finally:
                os.chdir(cwd)


class LedgerSyncOverSqliteTests(unittest.TestCase):
    def test_sync_resolves_market_data_from_sqlite(self) -> None:
        from baibai_loop.ledger.cli import _load_market_data

        asof = date(2026, 4, 24)
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = Path(tmpdir)
            sqlite_dir = workspace / "data" / "screening"
            sqlite_path = sqlite_dir / "market.sqlite"
            _populate_screening_fixture(sqlite_path, asof)

            # Place a minimal research packet so _load_market_data discovers
            # at least one decision date and triggers J-Quants resolution.
            research_path = workspace / "records" / "05-research" / f"{asof.isoformat()}-130A.md"
            research_path.parent.mkdir(parents=True)
            research_path.write_text("---\nticker: 130A\n---\n", encoding="utf-8")

            env = {"JQUANTS_API_KEY": "token"}

            calendar, bars, warnings = _load_market_data(workspace, env)

            self.assertEqual(warnings, ())
            self.assertGreater(len(calendar), 0)
            self.assertGreater(len(bars), 0)
            self.assertEqual(bars[0].ticker, "130A")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
