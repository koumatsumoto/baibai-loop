"""Screening publishes from SQLite without JSON files or provider HTTP calls.

The fixture supplies the rows each provider reads. HTTP requests are forbidden
and checked explicitly, including errors that the command might otherwise catch.
"""

from __future__ import annotations

from tests.helpers.master import insert_master_rows


import tempfile
import unittest
from contextlib import closing
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]

from tests.helpers.screening_sqlite import add_source_coverage, insert_financial_record

from baibai_engine.foundation.time import JST
from baibai_engine.market.providers.edinet import EDINETProvider
from baibai_engine.market.providers.jpx import JPXProvider
from baibai_engine.market.providers.jquants import JQuantsProvider
from baibai_engine.screening.cli.providers import ProviderBundle
from baibai_engine.screening.cli.run import run_command
from baibai_engine.screening.config import ScreeningConfig
from baibai_engine.screening.run_store import ScreeningRunReader, run_store_path
from baibai_engine.storage.observations.schema import open_connection


def _populate_screening_fixture(sqlite_path: Path, asof: date) -> None:
    """Write the minimum SQLite rows the screening run needs to produce a
    valid YAML for `asof` without hitting any external provider.
    """
    sqlite_path.parent.mkdir(parents=True, exist_ok=True)
    with closing(open_connection(sqlite_path)) as conn, conn:
        ticker = "130A"
        # Current screening needs 1200 days; normalized PER needs 2200 days of
        # split factors. Keep the fixture fixed to its requested as-of date.
        history_days_back = 2200
        forward_days = 60
        history_start = asof - timedelta(days=history_days_back)
        history_end = asof + timedelta(days=forward_days)
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
        insert_master_rows(conn, [(_master_row[0], _master_row[1], _master_row[2], _master_row[3], _master_row[4], '011') for _master_row in master_rows])
        add_source_coverage(
            conn,
            source="jquants_master_snapshots",
            coverage_key=f"get_eq_master:{asof.isoformat()}..{asof.isoformat()}",
            record_count=len(master_rows),
            min_date=asof.isoformat(),
            max_date=asof.isoformat(),
        )

        # Bars: the full declared synthetic history
        bars_rows = [
            (
                ticker,
                (history_start + timedelta(days=index)).isoformat(),
                float(100 + index),
                300_000_000.0,
            )
            for index in range(history_days)
        ]
        conn.executemany(
            "INSERT INTO jquants_daily_bars(ticker, traded_at, close, turnover_value) "
            "VALUES (?, ?, ?, ?)",
            bars_rows,
        )
        # Mark the API request bracket the screening run will use; the
        # JQuantsProvider chunks at 31 days but the coverage check tolerates a
        # single import that contains the full window.
        add_source_coverage(
            conn,
            source="jquants_daily_bars",
            coverage_key=f"get_eq_bars_daily_range:{bars_start.isoformat()}..{history_end.isoformat()}",
            record_count=history_days,
            min_date=bars_start.isoformat(),
            max_date=history_end.isoformat(),
        )

        # Construct source documents, not precomputed financial columns.
        fin_rows = [
            {
                "Code": f"{ticker}0",
                "DiscDate": (asof - timedelta(days=365 * years_ago - 30)).isoformat(),
                "DiscNo": f"fy-{asof.year - years_ago}",
                "DiscTime": "15:00:00",
                "DocType": "FYFinancialStatements_Consolidated_JP",
                "CurPerType": "FY",
                "CurFYSt": date(asof.year - years_ago - 1, 4, 1).isoformat(),
                "CurFYEn": date(asof.year - years_ago, 3, 31).isoformat(),
                "CurPerSt": date(asof.year - years_ago - 1, 4, 1).isoformat(),
                "CurPerEn": date(asof.year - years_ago, 3, 31).isoformat(),
                "EPS": eps,
            }
            for years_ago, eps in ((3, 10.0), (2, 20.0), (1, 30.0))
        ]
        fin_rows.append(
            {
                "Code": f"{ticker}0",
                "DiscDate": (asof - timedelta(days=15)).isoformat(),
                "DiscNo": "current-fy",
                "DiscTime": "15:00:00",
                "DocType": "FYFinancialStatements_Consolidated_JP",
                "CurPerType": "FY",
                "CurFYSt": date(asof.year - 1, 4, 1).isoformat(),
                "CurFYEn": date(asof.year, 3, 31).isoformat(),
                "CurPerSt": date(asof.year - 1, 4, 1).isoformat(),
                "CurPerEn": date(asof.year, 3, 31).isoformat(),
                "NxtFYSt": date(asof.year, 4, 1).isoformat(),
                "NxtFYEn": date(asof.year + 1, 3, 31).isoformat(),
                "EPS": 18.0,
                "BPS": 130.0,
                "ShOutFY": 400_000_000.0,
                "Sales": 1100.0,
                "OP": 110.0,
                "NxFEPS": 22.0,
            }
        )
        for record in fin_rows:
            insert_financial_record(conn, record)
        add_source_coverage(
            conn,
            source="jquants_fin_summaries",
            coverage_key=f"get_fin_summary_range:{fin_start.isoformat()}..{asof.isoformat()}",
            record_count=len(fin_rows),
            min_date=fin_start.isoformat(),
            max_date=asof.isoformat(),
        )

        # Earnings calendar — at least one normalized row is required; an empty
        # covered payload is treated as incomplete so read-through can repair it.
        earnings_date = asof + timedelta(days=7)
        conn.execute(
            "INSERT INTO jpx_earnings_calendar(announcement_date, ticker) VALUES (?, ?)",
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

        # Market calendar covers the same fixed window as the bar fixture.
        calendar_rows = [
            (
                (history_start + timedelta(days=index)).isoformat(),
                1,
            )
            for index in range(history_days)
        ]
        conn.executemany(
            "INSERT INTO jquants_market_calendar(day, holiday_division) VALUES (?, ?)",
            calendar_rows,
        )
        add_source_coverage(
            conn,
            source="jquants_market_calendar",
            coverage_key=f"get_mkt_calendar:{history_start.isoformat()}..{history_end.isoformat()}",
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
            coverage_key=asof.isoformat(),
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
            coverage_key=asof.isoformat(),
            record_count=0,
            min_date=asof.isoformat(),
            max_date=asof.isoformat(),
        )


class ScreeningRunOverSqliteTests(unittest.TestCase):
    def test_run_command_publishes_using_only_sqlite_cache(self) -> None:
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
                        sqlite_path=sqlite_path,
                    ),
                    edinet=EDINETProvider(
                        config.edinet_api_key,
                        config.cache_dir,
                        sqlite_path=sqlite_path,
                    ),
                    jpx=JPXProvider(
                        sqlite_path=sqlite_path,
                    ),
                )

                with patch(
                    "requests.sessions.Session.request",
                    side_effect=AssertionError("screening attempted provider HTTP"),
                ) as request:
                    exit_code = run_command(
                        asof,
                        config,
                        providers,
                        now=datetime(asof.year, asof.month, asof.day, 9, 0, tzinfo=JST),
                    )
                request.assert_not_called()

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
