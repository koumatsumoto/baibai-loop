"""Jobs maintenance windows and source-isolated failure handling."""

from __future__ import annotations

import contextlib
import io
import sqlite3
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

from tests.helpers.market_maintenance import (
    FakeEDINETProvider,
    FakeJPXProvider,
    FakeJQuantsProvider,
)

from baibai_engine.machine_analysis.observations.jquants_decode import JQuantsProviderError
from baibai_engine.machine_analysis.observations.prices.price_models import JQuantsDailyBar
from baibai_engine.machine_analysis.observations.stocks.models import SecurityMaster
from baibai_engine.storage.sqlite.source_cache.schema import open_connection
from baibai_jobs.maintenance.market.cache import (
    backfill_history_command,
    bootstrap_cache_command,
)
from baibai_jobs.maintenance.market.providers import ProviderBundle


class _CorruptSQLiteJQuantsProvider(FakeJQuantsProvider):
    def get_eq_master(self, requested_asof: date) -> list[SecurityMaster]:
        del requested_asof
        raise sqlite3.DatabaseError("file is not a database")


class SourceMaintenanceCliTests(unittest.TestCase):
    def test_bootstrap_cache_command_fails_jpx_bootstrap_failure(self) -> None:
        stderr = io.StringIO()
        jquants = FakeJQuantsProvider()
        with contextlib.redirect_stderr(stderr):
            exit_code = bootstrap_cache_command(
                asof_date=date(2026, 5, 8),
                providers=ProviderBundle(
                    edinet_refresh=FakeEDINETProvider(),
                    jpx_refresh=FakeJPXProvider(fail_bootstrap=True),
                    jquants_refresh=jquants,
                ),
                sqlite_path=Path("stores/market/market.sqlite"),
            )

        self.assertEqual(exit_code, 1)
        self.assertIn("JPXProviderError: missing jpx source", stderr.getvalue())

    def test_bootstrap_cache_command_fails_cleanly_on_sqlite_corruption(self) -> None:
        stderr = io.StringIO()
        jquants = _CorruptSQLiteJQuantsProvider()
        with contextlib.redirect_stderr(stderr):
            exit_code = bootstrap_cache_command(
                asof_date=date(2026, 5, 8),
                providers=ProviderBundle(
                    edinet_refresh=FakeEDINETProvider(),
                    jpx_refresh=FakeJPXProvider(),
                    jquants_refresh=jquants,
                ),
                sqlite_path=Path("stores/market/market.sqlite"),
            )

        self.assertEqual(exit_code, 1)
        self.assertIn("DatabaseError: file is not a database", stderr.getvalue())

    def test_bootstrap_cache_command_asof_uses_source_specific_windows(self) -> None:
        asof = date(2026, 5, 8)
        jquants = FakeJQuantsProvider()
        edinet = FakeEDINETProvider()
        jpx = FakeJPXProvider()
        buffer = io.StringIO()

        exit_code = bootstrap_cache_command(
            asof_date=asof,
            providers=ProviderBundle(
                edinet_refresh=edinet,
                jpx_refresh=jpx,
                jquants_refresh=jquants,
            ),
            sqlite_path=Path("stores/market/market.sqlite"),
            stdout=buffer,
        )

        self.assertEqual(exit_code, 0)
        self.assertIn("bootstrap-cache jquants daily_bars", buffer.getvalue())
        self.assertNotIn("bootstrap-cache edinet documents", buffer.getvalue())
        self.assertIn("bootstrap-cache done", buffer.getvalue())
        self.assertIn(("get_eq_master", asof, asof), jquants.calls)
        self.assertIn(("get_eq_bars_daily_range", asof - timedelta(days=2200), asof), jquants.calls)
        self.assertIn(("get_fin_summary_range", asof - timedelta(days=2200), asof), jquants.calls)
        self.assertEqual(
            jquants.calls.count(("get_eq_bars_daily_range", asof - timedelta(days=2200), asof)),
            1,
        )
        self.assertEqual(
            jquants.calls.count(("refresh_fin_summary_range", asof - timedelta(days=2200), asof)),
            1,
        )
        self.assertNotIn("get_adjustment_factor_bars_range", [call[0] for call in jquants.calls])
        self.assertNotIn("get_fy_summary_range", [call[0] for call in jquants.calls])
        # The calendar bootstrap fetches a forward window so the unattended daily
        # batch can read the current (and upcoming) business-day rows.
        self.assertIn(
            ("get_mkt_calendar", asof - timedelta(days=7), asof + timedelta(days=45)),
            jquants.calls,
        )
        self.assertNotIn("refresh_mkt_short_sale_report_range", [call[0] for call in jquants.calls])
        self.assertEqual(edinet.bootstrap_calls, [])
        self.assertEqual(jpx.bootstrap_calls, [asof])


def _seed_trading_days(sqlite_path: Path, start: date, end: date) -> None:
    """Weekday rows so the weekly-margin candidates have a calendar to derive from."""
    conn = open_connection(sqlite_path)
    try:
        day = start
        rows = []
        while day <= end:
            if day.weekday() < 5:
                rows.append(("7203", day.isoformat(), 1.0))
            day += timedelta(days=1)
        conn.executemany(
            "INSERT OR REPLACE INTO jquants_daily_bars(ticker, traded_at, close) VALUES (?, ?, ?)",
            rows,
        )
        conn.commit()
    finally:
        conn.close()


class BackfillHistoryTests(unittest.TestCase):
    def test_each_range_source_is_fetched_over_the_named_window(self) -> None:
        # The window is stated once rather than derived from an as-of, so a decade of
        # history costs one pass instead of one 1200-day re-fetch per cohort date.
        provider = FakeJQuantsProvider()
        output = io.StringIO()

        with tempfile.TemporaryDirectory() as tmp:
            sqlite_path = Path(tmp) / "market.sqlite"
            _seed_trading_days(sqlite_path, date(2016, 8, 1), date(2018, 3, 1))
            code = backfill_history_command(
                start=date(2016, 8, 1),
                end=date(2018, 3, 1),
                providers=ProviderBundle(jquants_refresh=provider),
                sqlite_path=sqlite_path,
                stdout=output,
            )

        self.assertEqual(code, 0)
        # The row readers answer with the whole window, so the three that can
        # span years are asked a year at a time. The interior boundaries follow the
        # calendar, not `start`, so a run naming a different first date reuses the
        # same fetch chunks. The calendar is one provider call and stays whole.
        by_source: dict[str, list[tuple[date, date]]] = {}
        for name, start, end in provider.calls:
            by_source.setdefault(name, []).append((start, end))
        year_spans = [
            (date(2016, 8, 1), date(2016, 12, 31)),
            (date(2017, 1, 1), date(2017, 12, 31)),
            (date(2018, 1, 1), date(2018, 3, 1)),
        ]
        self.assertEqual(by_source["get_eq_bars_daily_range"], year_spans)
        self.assertEqual(by_source["get_fin_summary_range"], year_spans)
        self.assertNotIn("get_mkt_short_sale_report_range", by_source)
        self.assertEqual(by_source["get_mkt_calendar"], [(date(2016, 8, 1), date(2018, 3, 1))])
        self.assertIn("backfill-history done", output.getvalue())

    def test_a_source_that_fails_does_not_stop_the_others(self) -> None:
        # A provider that cannot answer for one source says nothing about the rest,
        # and a run of this length should not lose hours of work to one refusal.
        class FailingBars(FakeJQuantsProvider):
            def get_eq_bars_daily_range(self, start: date, end: date) -> list[JQuantsDailyBar]:
                self.calls.append(("get_eq_bars_daily_range", start, end))
                raise JQuantsProviderError("bars unavailable")

        provider = FailingBars()
        errors = io.StringIO()
        with contextlib.redirect_stderr(errors):
            code = backfill_history_command(
                start=date(2016, 8, 1),
                end=date(2016, 8, 31),
                providers=ProviderBundle(jquants_refresh=provider),
                sqlite_path=Path("stores/market/market.sqlite"),
                stdout=io.StringIO(),
            )

        self.assertEqual(code, 1)
        self.assertIn("get_fin_summary_range", [call[0] for call in provider.calls])
        self.assertIn("get_mkt_calendar", [call[0] for call in provider.calls])
        self.assertIn("daily_bars", errors.getvalue())


def test_bootstrap_financial_failure_does_not_prevent_market_or_jpx(tmp_path: Path) -> None:
    class FailingFinancial(FakeJQuantsProvider):
        def refresh_fin_summary_range(self, *args, **kwargs):
            raise JQuantsProviderError("financial HTTP failure")

    jquants = FailingFinancial()
    edinet = FakeEDINETProvider()
    jpx = FakeJPXProvider()
    assert (
        bootstrap_cache_command(
            asof_date=date(2026, 5, 8),
            providers=ProviderBundle(
                edinet_refresh=edinet,
                jpx_refresh=jpx,
                jquants_refresh=jquants,
            ),
            sqlite_path=tmp_path / "market.sqlite",
            stdout=io.StringIO(),
        )
        == 1
    )
    names = [call[0] for call in jquants.calls]
    assert "get_mkt_calendar" in names
    assert "refresh_mkt_short_sale_report_range" not in names
    assert edinet.bootstrap_calls == []
    assert len(jpx.bootstrap_calls) == 1


def test_bootstrap_market_failure_does_not_prevent_financial_or_jpx(tmp_path: Path) -> None:
    jquants = _CorruptSQLiteJQuantsProvider()
    edinet = FakeEDINETProvider()
    jpx = FakeJPXProvider()
    assert (
        bootstrap_cache_command(
            asof_date=date(2026, 5, 8),
            providers=ProviderBundle(
                edinet_refresh=edinet,
                jpx_refresh=jpx,
                jquants_refresh=jquants,
            ),
            sqlite_path=tmp_path / "market.sqlite",
            stdout=io.StringIO(),
        )
        == 1
    )
    names = [call[0] for call in jquants.calls]
    assert names.count("refresh_fin_summary_range") == 1
    assert "get_fy_summary_range" not in names
    assert edinet.bootstrap_calls == []
    assert len(jpx.bootstrap_calls) == 1
