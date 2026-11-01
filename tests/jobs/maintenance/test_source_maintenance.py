"""Jobs maintenance windows and source-isolated failure handling."""

from __future__ import annotations

import contextlib
import io
import os
import sqlite3
import tempfile
import unittest
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from tests.helpers.market_maintenance import (
    FakeEDINETProvider,
    FakeJPXProvider,
    FakeJQuantsProvider,
)

from baibai_engine.machine_analysis.observations.jquants_decode import JQuantsProviderError
from baibai_engine.machine_analysis.observations.jquants_writer import (
    store_jquants_daily_bars,
    store_jquants_weekly_margin,
)
from baibai_engine.machine_analysis.observations.prices.price_models import JQuantsDailyBar
from baibai_engine.machine_analysis.observations.stocks.models import SecurityMaster
from baibai_engine.primitives.time import JST
from baibai_engine.storage.sqlite.source_cache.schema import open_connection
from baibai_jobs.maintenance.market import cli as source_app
from baibai_jobs.maintenance.market.cache import (
    backfill_history_command,
    backfill_master_command,
    bootstrap_cache_command,
)
from baibai_jobs.maintenance.market.providers import ProviderBundle
from baibai_jobs.maintenance.source_refresh.jquants import JQuantsRefresh


@dataclass
class _FailingOnDateJQuantsProvider(FakeJQuantsProvider):
    failing_date: date | None = None

    def get_eq_master(self, requested_asof: date) -> list[SecurityMaster]:
        if requested_asof == self.failing_date:
            raise JQuantsProviderError(f"no master for {requested_asof.isoformat()}")
        return super().get_eq_master(requested_asof)


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
        self.assertIn("bootstrap-cache edinet documents", buffer.getvalue())
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
        self.assertIn(
            ("refresh_mkt_short_sale_report_range", asof - timedelta(days=7), asof),
            jquants.calls,
        )
        self.assertEqual(edinet.bootstrap_calls, [(asof - timedelta(days=730), asof)])
        self.assertEqual(jpx.bootstrap_calls, [asof])

    def test_bootstrap_refreshes_the_final_legacy_week_after_publication(self) -> None:
        asof = date(2026, 9, 25)
        jquants = FakeJQuantsProvider()
        with tempfile.TemporaryDirectory() as tmp:
            sqlite_path = Path(tmp) / "market.sqlite"
            trading_days = (
                date(2026, 9, 14),
                date(2026, 9, 15),
                date(2026, 9, 16),
                date(2026, 9, 17),
                date(2026, 9, 18),
                date(2026, 9, 22),
                date(2026, 9, 24),
                date(2026, 9, 25),
            )
            store_jquants_daily_bars(
                sqlite_path,
                [{"Date": day.isoformat(), "Code": "72030", "C": 100.0} for day in trading_days],
                requested_start=trading_days[0],
                requested_end=trading_days[-1],
            )

            exit_code = bootstrap_cache_command(
                asof_date=asof,
                providers=ProviderBundle(
                    edinet_refresh=FakeEDINETProvider(),
                    jpx_refresh=FakeJPXProvider(),
                    jquants_refresh=jquants,
                ),
                sqlite_path=sqlite_path,
                stdout=io.StringIO(),
            )

        self.assertEqual(exit_code, 0)
        self.assertIn(
            (
                "refresh_mkt_margin_interest_week",
                date(2026, 9, 18),
                date(2026, 9, 18),
            ),
            jquants.calls,
        )

    def test_bootstrap_repairs_an_empty_week_cached_before_publication(self) -> None:
        asof = date(2026, 8, 5)
        week_end = date(2026, 7, 31)
        jquants = FakeJQuantsProvider()
        with tempfile.TemporaryDirectory() as tmp:
            sqlite_path = Path(tmp) / "market.sqlite"
            trading_days = (
                week_end,
                date(2026, 8, 3),
                date(2026, 8, 4),
                asof,
            )
            store_jquants_daily_bars(
                sqlite_path,
                [{"Date": day.isoformat(), "Code": "72030", "C": 100.0} for day in trading_days],
                requested_start=trading_days[0],
                requested_end=trading_days[-1],
            )
            store_jquants_weekly_margin(sqlite_path, [], week_end=week_end)
            conn = open_connection(sqlite_path)
            try:
                conn.execute(
                    "UPDATE source_coverage SET fetched_at_utc = ? "
                    "WHERE source = 'jquants_weekly_margin'",
                    ("2026-08-04T07:00:00+00:00",),
                )
                conn.commit()
            finally:
                conn.close()

            exit_code = bootstrap_cache_command(
                asof_date=asof,
                providers=ProviderBundle(
                    edinet_refresh=FakeEDINETProvider(),
                    jpx_refresh=FakeJPXProvider(),
                    jquants_refresh=jquants,
                ),
                sqlite_path=sqlite_path,
                stdout=io.StringIO(),
            )

        self.assertEqual(exit_code, 0)
        self.assertIn(
            ("refresh_mkt_margin_interest_week", week_end, week_end),
            jquants.calls,
        )

    def test_backfill_master_fetches_only_the_master_for_each_date(self) -> None:
        # 1 cohort あたり 1 request だけを使うことが、この命令の存在理由である。
        jquants = FakeJQuantsProvider()
        buffer = io.StringIO()
        dates = [date(2026, 4, 30), date(2026, 5, 29)]

        with tempfile.TemporaryDirectory() as tmpdir:
            exit_code = backfill_master_command(
                asof_dates=dates,
                providers=ProviderBundle(jquants_refresh=jquants),
                sqlite_path=Path(tmpdir) / "market.sqlite",
                stdout=buffer,
            )

        self.assertEqual(exit_code, 0)
        self.assertEqual(jquants.calls, [("get_eq_master", day, day) for day in dates])
        self.assertIn("backfill-master start: 2 date(s)", buffer.getvalue())
        # 既存 snapshot が無いので fetch 側として報告される。
        self.assertIn("2026-04-30: fetched", buffer.getvalue())
        self.assertIn("backfill-master done", buffer.getvalue())

    def test_backfill_master_continues_past_a_failed_date_and_fails_overall(self) -> None:
        # 1 日の取得失敗で残りの cohort を諦めないが、成功したことにもしない。
        jquants = _FailingOnDateJQuantsProvider(failing_date=date(2026, 4, 30))
        buffer = io.StringIO()
        stderr = io.StringIO()

        with contextlib.redirect_stderr(stderr), tempfile.TemporaryDirectory() as tmpdir:
            exit_code = backfill_master_command(
                asof_dates=[date(2026, 4, 30), date(2026, 5, 29)],
                providers=ProviderBundle(jquants_refresh=jquants),
                sqlite_path=Path(tmpdir) / "market.sqlite",
                stdout=buffer,
            )

        self.assertEqual(exit_code, 1)
        self.assertIn("1 of 2 date(s) failed", stderr.getvalue())
        self.assertIn(("get_eq_master", date(2026, 5, 29), date(2026, 5, 29)), jquants.calls)
        self.assertNotIn("backfill-master done", buffer.getvalue())

    def _backfill_master_main(self, argv: list[str]) -> tuple[int, str]:
        """Run backfill-master through main() with no reachable provider.

        The guards under test all reject before any fetch, so a provider that
        raises on use proves the rejection happened for the stated reason.
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            cwd = Path.cwd()
            stderr = io.StringIO()
            try:
                os.chdir(Path(tmpdir))
                with (
                    patch.dict(os.environ, {"JQUANTS_API_KEY": "token"}),
                    patch.object(
                        JQuantsRefresh,
                        "_fetch",
                        side_effect=AssertionError("provider fetch must not be used"),
                    ),
                    contextlib.redirect_stderr(stderr),
                ):
                    exit_code = source_app.main(["backfill-master", *argv])
            finally:
                os.chdir(cwd)
        return exit_code, stderr.getvalue()

    def test_main_backfill_master_rejects_a_month_end_range_given_backwards(self) -> None:
        # 空の grid を「対象が無い」と読ませず、引数の取り違えとして名指しする。
        exit_code, stderr = self._backfill_master_main(
            ["--month-end-from", "2025-06-30", "--month-end-to", "2024-01-31"]
        )

        self.assertEqual(exit_code, 1)
        self.assertIn("2025-06-30 is after --month-end-to 2024-01-31", stderr)

    def test_main_backfill_master_rejects_one_sided_month_end_range(self) -> None:
        exit_code, stderr = self._backfill_master_main(["--month-end-from", "2025-06-30"])

        self.assertEqual(exit_code, 1)
        self.assertIn("must be given together", stderr)

    def test_main_backfill_master_rejects_a_future_asof(self) -> None:
        # 未来日の断面は存在しない。要求日を echo する provider があれば、当日の
        # population が別日の断面として永続化されてしまう。
        future = (datetime.now(JST).date() + timedelta(days=1)).isoformat()
        exit_code, stderr = self._backfill_master_main(["--asof", future])

        self.assertEqual(exit_code, 1)
        self.assertIn("rejects future dates", stderr)
        self.assertIn(future, stderr)

    def test_main_backfill_master_allows_today_before_bars_when_calendar_is_open(self) -> None:
        today = datetime.now(JST).date()
        called = []
        with (
            patch.object(source_app, "market_calendar_business_day", return_value=True),
            patch(
                "baibai_engine.machine_analysis.historical_evaluation.grid.days_with_bars",
                return_value=set(),
            ),
            patch.object(
                source_app,
                "backfill_master_command",
                side_effect=lambda **kwargs: called.append(kwargs["asof_dates"]) or 0,
            ),
        ):
            exit_code, _ = self._backfill_master_main(["--asof", today.isoformat()])
        self.assertEqual(exit_code, 0)
        self.assertEqual(called, [[today]])

    def test_main_backfill_master_skips_today_on_closed_market(self) -> None:
        today = datetime.now(JST).date()
        with (
            patch.object(source_app, "market_calendar_business_day", return_value=False),
            patch.object(
                source_app, "backfill_master_command", side_effect=AssertionError("no provider")
            ),
        ):
            exit_code, _ = self._backfill_master_main(["--asof", today.isoformat()])
        self.assertEqual(exit_code, 0)

    def test_main_backfill_master_fails_closed_on_missing_or_broken_calendar(self) -> None:
        today = datetime.now(JST).date()
        for value in (None, sqlite3.OperationalError("broken")):
            effect = {"return_value": value} if value is None else {"side_effect": value}
            with (
                patch.object(source_app, "market_calendar_business_day", **effect),
                patch.object(
                    source_app,
                    "backfill_master_command",
                    side_effect=AssertionError("no provider"),
                ),
            ):
                exit_code, _ = self._backfill_master_main(["--asof", today.isoformat()])
            self.assertEqual(exit_code, 1)

    def test_main_backfill_master_mixed_dates_skip_only_closed_today(self) -> None:
        today = datetime.now(JST).date()
        historical = date(2026, 9, 18)
        called = []
        with (
            patch.object(source_app, "market_calendar_business_day", return_value=False),
            patch(
                "baibai_engine.machine_analysis.historical_evaluation.grid.days_with_bars",
                return_value={historical},
            ),
            patch.object(
                source_app,
                "backfill_master_command",
                side_effect=lambda **kwargs: called.append(kwargs["asof_dates"]) or 0,
            ),
        ):
            exit_code, _ = self._backfill_master_main(
                ["--asof", historical.isoformat(), "--asof", today.isoformat()]
            )
        self.assertEqual(exit_code, 0)
        self.assertEqual(called, [[historical]])

    def test_main_backfill_master_rejects_a_date_the_bar_store_never_priced(self) -> None:
        # この過去日取得の入口は保存済みの取引日を要求する。
        # 価格未保存だけで取引所の休場やproviderの断面不存在まで断定しない。
        exit_code, stderr = self._backfill_master_main(["--asof", "2026-01-01"])

        self.assertEqual(exit_code, 1)
        self.assertIn("does not show as trading days", stderr)

    def test_main_backfill_master_requires_at_least_one_date(self) -> None:
        exit_code, stderr = self._backfill_master_main([])

        self.assertEqual(exit_code, 1)
        self.assertIn("resolved no dates", stderr)


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
    def test_normal_backfill_fetches_daily_margin_after_the_source_boundary(self) -> None:
        provider = FakeJQuantsProvider()
        with tempfile.TemporaryDirectory() as tmp:
            sqlite_path = Path(tmp) / "market.sqlite"
            _seed_trading_days(sqlite_path, date(2026, 9, 25), date(2026, 9, 28))
            code = backfill_history_command(
                start=date(2026, 9, 24),
                end=date(2026, 9, 28),
                providers=ProviderBundle(jquants_refresh=provider),
                sqlite_path=sqlite_path,
                stdout=io.StringIO(),
            )

        self.assertEqual(code, 0)
        self.assertEqual(
            [call for call in provider.calls if call[0] == "get_mkt_all_issues_daily_margin"],
            [
                ("get_mkt_all_issues_daily_margin", date(2026, 9, 25), date(2026, 9, 25)),
                ("get_mkt_all_issues_daily_margin", date(2026, 9, 28), date(2026, 9, 28)),
            ],
        )

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
        self.assertEqual(by_source["get_mkt_short_sale_report_range"], year_spans)
        self.assertEqual(by_source["get_mkt_calendar"], [(date(2016, 8, 1), date(2018, 3, 1))])
        self.assertIn("backfill-history done", output.getvalue())

    def test_a_historical_window_keeps_its_final_week_without_later_cached_bars(self) -> None:
        # `end` here is a named past boundary, not today, so its week is complete
        # and holds a real balance date. Treating it as in-progress would drop the
        # only candidate and report a backfill that fetched nothing as success.
        provider = FakeJQuantsProvider()
        with tempfile.TemporaryDirectory() as tmp:
            sqlite_path = Path(tmp) / "market.sqlite"
            _seed_trading_days(sqlite_path, date(2026, 7, 1), date(2026, 7, 24))
            output = io.StringIO()
            code = backfill_history_command(
                start=date(2026, 7, 20),
                end=date(2026, 7, 24),
                providers=ProviderBundle(jquants_refresh=provider),
                sqlite_path=sqlite_path,
                stdout=output,
            )

        self.assertEqual(code, 0)
        weeks = [
            start for name, start, _ in provider.calls if name == "get_mkt_margin_interest_week"
        ]
        self.assertEqual(weeks, [date(2026, 7, 24)])

    def test_a_window_with_no_trading_days_fails_rather_than_reporting_success(self) -> None:
        provider = FakeJQuantsProvider()
        errors = io.StringIO()
        with tempfile.TemporaryDirectory() as tmp:
            sqlite_path = Path(tmp) / "market.sqlite"
            _seed_trading_days(sqlite_path, date(2026, 7, 1), date(2026, 7, 31))
            with contextlib.redirect_stderr(errors):
                code = backfill_history_command(
                    start=date(2026, 7, 25),
                    end=date(2026, 7, 26),
                    providers=ProviderBundle(
                        jquants_refresh=provider,
                    ),
                    sqlite_path=sqlite_path,
                    stdout=io.StringIO(),
                )

        self.assertEqual(code, 1)
        self.assertIn("no weekly margin balance date candidates", errors.getvalue())

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


def test_bootstrap_financial_failure_does_not_prevent_market_edinet_or_jpx(tmp_path: Path) -> None:
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
    assert "refresh_mkt_short_sale_report_range" in names
    assert len(edinet.bootstrap_calls) == len(jpx.bootstrap_calls) == 1


def test_bootstrap_market_failure_does_not_prevent_financial_edinet_or_jpx(tmp_path: Path) -> None:
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
    assert len(edinet.bootstrap_calls) == len(jpx.bootstrap_calls) == 1
