from __future__ import annotations

import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

from tests.helpers.screening_sqlite import insert_daily_bars_from_closes

from baibai_engine.market.bars import JQuantsAdjustmentFactorEvent
from baibai_engine.market.benchmark import TOPIX_ETF_PROXY
from baibai_engine.screening.calibration.forward import (
    HORIZONS,
    ControlEventExit,
    FailureExit,
    ForwardReturnRow,
    _FYDividendObservation,
    _ticker_forward_rows,
    compute_forward_returns,
    is_failure_delisting,
    read_control_event_exits,
    read_failure_exits,
)
from baibai_engine.screening.calibration.grid import complete_month_end_dates
from baibai_engine.screening.providers.jquants import JQuantsDailyBar
from baibai_engine.screening.sqlite_cache import open_connection


def _bar(
    day: date, close: float, factor: float | None = None, *, ticker: str = "1000"
) -> JQuantsDailyBar:
    return JQuantsDailyBar(
        ticker=ticker,
        traded_at=day,
        close=close,
        turnover_value=None,
        adjustment_factor=factor,
    )


class MonthEndDatesTest(unittest.TestCase):
    def test_complete_month_end_dates_picks_last_qualifying_day_per_month(self) -> None:
        day_counts = [
            (date(2025, 1, 30), 4000),
            (date(2025, 1, 31), 4100),
            (date(2025, 2, 27), 4000),
            # 部分データ日 (取込途中断面) は月末営業日として選ばない。
            (date(2025, 2, 28), 100),
            # 翌月の営業日 = 1-2 月が完全月であることの証拠 (3 月自身は最終
            # data 月なので cohort にならない)。
            (date(2025, 3, 3), 4100),
        ]
        self.assertEqual(
            complete_month_end_dates(day_counts, end=date(2025, 3, 31), min_tickers=2000),
            [date(2025, 1, 31), date(2025, 2, 27)],
        )

    def test_complete_month_end_dates_drops_month_truncated_by_end(self) -> None:
        # end が 2 月の途中 → 2/13 を「月末」と誤認せず 2 月を cohort から落とす。
        day_counts = [
            (date(2025, 1, 31), 4100),
            (date(2025, 2, 13), 4000),
            (date(2025, 2, 27), 4000),
            (date(2025, 3, 3), 4100),
        ]
        self.assertEqual(
            complete_month_end_dates(day_counts, end=date(2025, 2, 14), min_tickers=2000),
            [date(2025, 1, 31)],
        )

    def test_complete_month_end_dates_empty_when_all_below_threshold(self) -> None:
        self.assertEqual(
            complete_month_end_dates(
                [(date(2025, 1, 31), 10)], end=date(2025, 1, 31), min_tickers=2000
            ),
            [],
        )


class ForwardReturnTest(unittest.TestCase):
    def test_dividend_guard_keeps_price_return_resolved(self) -> None:
        row = _ticker_forward_rows(
            "1000",
            [
                _bar(date(2025, 1, 31), 1000.0, 1.0),
                _bar(date(2026, 1, 31), 1000.0, 1.0),
            ],
            adjustment_events=[JQuantsAdjustmentFactorEvent("1000", date(2024, 10, 4), 0.5)],
            fy_dividends=[
                _FYDividendObservation(
                    fiscal_year_end=date(2025, 3, 31),
                    disclosed_at=date(2025, 5, 15),
                    dps_actual_annual=60.0,
                    period_start=date(2024, 4, 1),
                    payments=(None, 20.0, None, 40.0),
                )
            ],
            asofs=[date(2025, 1, 31)],
            horizons=(HORIZONS["1y"],),
            eval_cap=date(2026, 2, 1),
        )[0]
        self.assertTrue(row.resolved)
        self.assertEqual(row.status, "resolved")
        self.assertEqual(row.price_return, 0.0)
        self.assertEqual(row.total_return_status, "unresolved_dividend_split_basis")
        self.assertIsNone(row.total_return)
        self.assertIsNone(row.realized_dividend_sum)

    def test_forward_return_plain_price_move(self) -> None:
        bars = [
            _bar(date(2025, 1, 31), 100.0),
            _bar(date(2025, 4, 30), 120.0),
            _bar(date(2025, 7, 31), 150.0),
        ]
        rows = _ticker_forward_rows(
            "1000",
            bars,
            asofs=[date(2025, 1, 31)],
            horizons=(HORIZONS["3m"], HORIZONS["6m"]),
            eval_cap=date(2025, 7, 31),
        )
        by_horizon = {row.horizon: row for row in rows}
        row_3m = by_horizon["3m"]
        self.assertTrue(row_3m.resolved)
        assert row_3m.price_return is not None
        self.assertAlmostEqual(row_3m.price_return, 0.2)
        row_6m = by_horizon["6m"]
        self.assertTrue(row_6m.resolved)
        assert row_6m.price_return is not None
        self.assertAlmostEqual(row_6m.price_return, 0.5)

    def test_forward_return_normalizes_split_between_entry_and_exit(self) -> None:
        # 1:2 分割 (factor 0.5)。分割前 100 円 → 分割後 60 円は
        # asof-basis で 100 → 120 相当 = +20%。素の close 比 (60/100-1=-40%)
        # にならないことを確認する。
        bars = [
            _bar(date(2025, 1, 31), 100.0),
            _bar(date(2025, 3, 3), 52.0, factor=0.5),
            _bar(date(2025, 4, 30), 60.0),
        ]
        rows = _ticker_forward_rows(
            "1000",
            bars,
            asofs=[date(2025, 1, 31)],
            horizons=(HORIZONS["3m"],),
            eval_cap=date(2025, 5, 30),
        )
        row = rows[0]
        self.assertTrue(row.resolved)
        assert row.price_return is not None
        self.assertAlmostEqual(row.price_return, 60.0 / 50.0 - 1)

    def test_forward_return_keeps_adjustment_event_without_close(self) -> None:
        """取引停止日のfactorを価格barから独立に読み、100:1併合をリターンにしない。"""

        bars = [
            _bar(date(2025, 1, 31), 1.0),
            _bar(date(2025, 4, 30), 100.0),
        ]
        events = [JQuantsAdjustmentFactorEvent("1000", date(2025, 3, 3), 100.0)]

        fixed = _ticker_forward_rows(
            "1000",
            bars,
            adjustment_events=events,
            asofs=[date(2025, 1, 31)],
            horizons=(HORIZONS["3m"],),
            eval_cap=date(2025, 4, 30),
        )[0]
        mutation = _ticker_forward_rows(
            "1000",
            bars,
            adjustment_events=(),
            asofs=[date(2025, 1, 31)],
            horizons=(HORIZONS["3m"],),
            eval_cap=date(2025, 4, 30),
        )[0]

        self.assertEqual(fixed.price_return, 0.0)
        self.assertEqual(mutation.price_return, 99.0)

    def test_forward_reader_keeps_adjustment_event_without_close(self) -> None:
        """DB reader自体がclose欠損eventをforward計算へ渡すことを固定する。"""

        with tempfile.TemporaryDirectory() as tmp:
            sqlite_path = Path(tmp) / "market.sqlite"
            conn = open_connection(sqlite_path)
            try:
                conn.executemany(
                    "INSERT INTO jquants_daily_bars("
                    "ticker, traded_at, close, adjustment_factor"
                    ") VALUES (?, ?, ?, ?)",
                    [
                        ("1000", "2025-01-31", 1.0, 1.0),
                        ("1000", "2025-03-03", None, 100.0),
                        ("1000", "2025-04-30", 100.0, 1.0),
                    ],
                )
                conn.commit()
            finally:
                conn.close()

            fixed = next(
                row
                for row in compute_forward_returns(
                    sqlite_path,
                    asofs=[date(2025, 1, 31)],
                    tickers=["1000"],
                    horizons=["3m"],
                    control_event_exits={},
                    failure_exits={},
                )
                if row.ticker == "1000"
            )
            conn = open_connection(sqlite_path)
            try:
                conn.execute(
                    "UPDATE jquants_daily_bars SET adjustment_factor = 1 "
                    "WHERE ticker = ? AND traded_at = ?",
                    ("1000", "2025-03-03"),
                )
                conn.commit()
            finally:
                conn.close()
            mutation = next(
                row
                for row in compute_forward_returns(
                    sqlite_path,
                    asofs=[date(2025, 1, 31)],
                    tickers=["1000"],
                    horizons=["3m"],
                    control_event_exits={},
                    failure_exits={},
                )
                if row.ticker == "1000"
            )

            self.assertEqual(fixed.price_return, 0.0)
            self.assertEqual(mutation.price_return, 99.0)

    def test_forward_reader_loads_events_from_the_first_selected_fiscal_year(self) -> None:
        """entry前でも対象FY内のfactorは支払別DPS換算に必要である。"""

        with tempfile.TemporaryDirectory() as tmp:
            sqlite_path = Path(tmp) / "market.sqlite"
            conn = open_connection(sqlite_path)
            try:
                conn.executemany(
                    "INSERT INTO jquants_daily_bars("
                    "ticker, traded_at, close, adjustment_factor"
                    ") VALUES (?, ?, ?, ?)",
                    [
                        ("1000", "2019-08-01", None, 0.5),
                        ("1000", "2020-01-31", 100.0, 1.0),
                        ("1000", "2021-01-29", 100.0, 1.0),
                        ("1000", "2021-02-01", 100.0, 1.0),
                    ],
                )
                conn.execute(
                    "INSERT INTO jquants_fin_summaries("
                    "ticker, disclosed_at, fiscal_period, fiscal_year_end, period_start, "
                    "dps_actual_annual, dividend_q1, dividend_interim"
                    ") VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        "1000",
                        "2020-05-12",
                        "FY",
                        "2020-03-31",
                        "2019-04-01",
                        75.0,
                        50.0,
                        25.0,
                    ),
                )
                conn.commit()
            finally:
                conn.close()

            fixed = next(
                row
                for row in compute_forward_returns(
                    sqlite_path,
                    asofs=[date(2020, 1, 31)],
                    tickers=["1000"],
                    horizons=["1y"],
                    control_event_exits={},
                    failure_exits={},
                )
                if row.ticker == "1000"
            )
            conn = open_connection(sqlite_path)
            try:
                conn.execute(
                    "UPDATE jquants_daily_bars SET adjustment_factor = 1 "
                    "WHERE ticker = ? AND traded_at = ?",
                    ("1000", "2019-08-01"),
                )
                conn.commit()
            finally:
                conn.close()
            mutation = next(
                row
                for row in compute_forward_returns(
                    sqlite_path,
                    asofs=[date(2020, 1, 31)],
                    tickers=["1000"],
                    horizons=["1y"],
                    control_event_exits={},
                    failure_exits={},
                )
                if row.ticker == "1000"
            )

            self.assertEqual(fixed.total_return_status, "resolved")
            self.assertEqual(fixed.realized_dividend_sum, 50.0)
            self.assertEqual(fixed.total_return, 0.5)
            self.assertEqual(mutation.realized_dividend_sum, 75.0)
            self.assertEqual(mutation.total_return, 0.75)

    def test_forward_reader_uses_february_month_end_for_missing_period_start(self) -> None:
        # 2025-02-28 FY のfallback開始日は閏年の2024-02-29。
        # 開始日当日のeventは期間外なので、明細を要求せず実績年間DPSを採れる。
        with tempfile.TemporaryDirectory() as tmp:
            sqlite_path = Path(tmp) / "market.sqlite"
            conn = open_connection(sqlite_path)
            try:
                conn.executemany(
                    "INSERT INTO jquants_daily_bars("
                    "ticker, traded_at, close, adjustment_factor"
                    ") VALUES (?, ?, ?, ?)",
                    [
                        ("1000", "2024-02-29", None, 0.5),
                        ("1000", "2025-01-31", 1000.0, 1.0),
                        ("1000", "2026-01-30", 1000.0, 1.0),
                        ("1000", "2026-02-02", 1000.0, 1.0),
                    ],
                )
                conn.execute(
                    "INSERT INTO jquants_fin_summaries("
                    "ticker, disclosed_at, fiscal_period, fiscal_year_end, dps_actual_annual"
                    ") VALUES (?, ?, ?, ?, ?)",
                    ("1000", "2025-05-15", "FY", "2025-02-28", 60.0),
                )
                conn.commit()
            finally:
                conn.close()
            row = next(
                row
                for row in compute_forward_returns(
                    sqlite_path,
                    asofs=[date(2025, 1, 31)],
                    tickers=["1000"],
                    horizons=["1y"],
                    control_event_exits={},
                    failure_exits={},
                )
                if row.ticker == "1000"
            )
            self.assertEqual(row.status, "resolved")
            self.assertEqual(row.price_return, 0.0)
            self.assertEqual(row.total_return_status, "resolved")
            self.assertEqual(row.realized_dividend_sum, 60.0)
            self.assertEqual(row.total_return, 0.06)

    def test_forward_return_unresolved_beyond_eval_cap(self) -> None:
        bars = [_bar(date(2025, 1, 31), 100.0), _bar(date(2025, 3, 31), 110.0)]
        rows = _ticker_forward_rows(
            "1000",
            bars,
            asofs=[date(2025, 1, 31)],
            horizons=(HORIZONS["1y"],),
            eval_cap=date(2025, 3, 31),
        )
        self.assertFalse(rows[0].resolved)
        self.assertIsNone(rows[0].price_return)

    def test_forward_return_flags_stale_exit_price(self) -> None:
        # 長期停止: stale exit は明示的な未解決であり resolved metric へ入れない。
        bars = [_bar(date(2025, 1, 31), 100.0), _bar(date(2025, 2, 28), 130.0)]
        rows = _ticker_forward_rows(
            "1000",
            bars,
            asofs=[date(2025, 1, 31)],
            horizons=(HORIZONS["3m"],),
            eval_cap=date(2025, 6, 30),
        )
        row = rows[0]
        self.assertFalse(row.resolved)
        self.assertTrue(row.stale_price)
        self.assertEqual(row.status, "unresolved_stale_exit")
        self.assertIsNone(row.price_return)

    def test_forward_return_invalid_entry_when_no_recent_bar_at_asof(self) -> None:
        # asof より 15 日超前の bar しか無い (上場前 / 廃止後) → 全 horizon 未解決。
        bars = [_bar(date(2024, 10, 31), 100.0)]
        rows = _ticker_forward_rows(
            "1000",
            bars,
            asofs=[date(2025, 1, 31)],
            horizons=tuple(HORIZONS.values()),
            eval_cap=date(2026, 6, 30),
        )
        self.assertEqual(len(rows), len(HORIZONS))
        self.assertTrue(all(not row.resolved for row in rows))

    def test_total_return_sums_latest_actual_dps_once_per_fiscal_year(self) -> None:
        bars = [
            _bar(date(2020, 1, 31), 2500.0, 1.0, ticker="7203"),
            _bar(date(2023, 1, 31), 3000.0, 1.0, ticker="7203"),
        ]
        dividends = [
            _FYDividendObservation(date(2020, 3, 31), date(2020, 5, 12), 50.0),
            _FYDividendObservation(date(2021, 3, 31), date(2021, 5, 12), 55.0),
            # 同じ FY の訂正は最新 non-null だけを使い、55+60 と二重加算しない。
            _FYDividendObservation(date(2021, 3, 31), date(2021, 6, 1), 60.0),
            _FYDividendObservation(date(2022, 3, 31), date(2022, 5, 11), 70.0),
            # exit 後の FY は対象外。
            _FYDividendObservation(date(2023, 3, 31), date(2023, 5, 10), 75.0),
        ]

        row = _ticker_forward_rows(
            "7203",
            bars,
            fy_dividends=dividends,
            asofs=[date(2020, 1, 31)],
            horizons=(HORIZONS["3y"],),
            eval_cap=date(2023, 6, 30),
        )[0]

        self.assertEqual(row.total_return_status, "resolved")
        self.assertEqual(row.realized_dividend_fy_count, 3)
        self.assertEqual(row.realized_dividend_sum, 180.0)
        self.assertAlmostEqual(row.price_return or 0.0, 3000.0 / 2500.0 - 1)
        self.assertAlmostEqual(row.total_return or 0.0, 3000.0 / 2500.0 - 1 + 180 / 2500)

    def test_total_return_normalizes_dps_to_adjusted_entry_basis(self) -> None:
        bars = [
            _bar(date(2021, 1, 31), 100.0, 1.0, ticker="7203"),
            _bar(date(2022, 1, 31), 120.0, 1.0, ticker="7203"),
            # exit 後の 1:2 split も store の最終 bar basis へ entry/exit/DPS を揃える。
            _bar(date(2022, 3, 1), 60.0, 0.5, ticker="7203"),
        ]
        row = _ticker_forward_rows(
            "7203",
            bars,
            fy_dividends=[_FYDividendObservation(date(2021, 3, 31), date(2021, 5, 12), 40.0)],
            asofs=[date(2021, 1, 31)],
            horizons=(HORIZONS["1y"],),
            eval_cap=date(2022, 3, 1),
        )[0]

        self.assertEqual(row.total_return_status, "resolved")
        self.assertEqual(row.realized_dividend_sum, 20.0)
        self.assertAlmostEqual(row.price_return or 0.0, 60.0 / 50.0 - 1)
        self.assertAlmostEqual(row.total_return or 0.0, 60.0 / 50.0 - 1 + 20.0 / 50.0)

    def test_total_return_uses_close_null_event_for_price_and_dps_basis(self) -> None:
        bars = [
            _bar(date(2021, 1, 31), 100.0, 1.0, ticker="7203"),
            _bar(date(2022, 1, 31), 120.0, 1.0, ticker="7203"),
        ]
        events = [JQuantsAdjustmentFactorEvent("7203", date(2022, 3, 1), 0.5)]

        row = _ticker_forward_rows(
            "7203",
            bars,
            adjustment_events=events,
            fy_dividends=[_FYDividendObservation(date(2021, 3, 31), date(2021, 5, 12), 40.0)],
            asofs=[date(2021, 1, 31)],
            horizons=(HORIZONS["1y"],),
            eval_cap=date(2022, 3, 1),
        )[0]

        self.assertEqual(row.total_return_status, "resolved")
        self.assertEqual(row.realized_dividend_sum, 20.0)
        self.assertAlmostEqual(row.price_return or 0.0, 60.0 / 50.0 - 1)
        self.assertAlmostEqual(row.total_return or 0.0, 60.0 / 50.0 - 1 + 20.0 / 50.0)

    def test_total_return_keeps_absent_null_zero_and_negative_dividends_distinct(self) -> None:
        bars = [
            _bar(date(2021, 1, 31), 100.0, 1.0, ticker="7203"),
            _bar(date(2022, 1, 31), 110.0, 1.0, ticker="7203"),
        ]
        cases = (
            ((), "unresolved_no_fy_observation", None),
            (
                (_FYDividendObservation(date(2021, 3, 31), date(2021, 5, 12), None),),
                "unresolved_missing_dividend",
                None,
            ),
            (
                (_FYDividendObservation(date(2021, 3, 31), date(2021, 5, 12), -1.0),),
                "unresolved_invalid_dividend",
                None,
            ),
            (
                (_FYDividendObservation(date(2021, 3, 31), date(2021, 5, 12), 0.0),),
                "resolved",
                0.1,
            ),
        )
        for dividends, expected_status, expected_total in cases:
            with self.subTest(status=expected_status, dividends=dividends):
                row = _ticker_forward_rows(
                    "7203",
                    bars,
                    fy_dividends=dividends,
                    asofs=[date(2021, 1, 31)],
                    horizons=(HORIZONS["1y"],),
                    eval_cap=date(2022, 1, 31),
                )[0]
                self.assertEqual(row.total_return_status, expected_status)
                if expected_total is None:
                    self.assertIsNone(row.realized_dividend_sum)
                    self.assertIsNone(row.total_return)
                else:
                    self.assertEqual(row.realized_dividend_sum, 0.0)
                    self.assertAlmostEqual(row.total_return or 0.0, expected_total)

    def test_total_return_rejects_latest_invalid_correction_instead_of_using_old_dps(self) -> None:
        bars = [
            _bar(date(2021, 1, 31), 100.0, 1.0, ticker="7203"),
            _bar(date(2022, 1, 31), 110.0, 1.0, ticker="7203"),
        ]
        for invalid in (-1.0, float("inf")):
            with self.subTest(invalid=invalid):
                row = _ticker_forward_rows(
                    "7203",
                    bars,
                    fy_dividends=[
                        _FYDividendObservation(date(2021, 3, 31), date(2021, 5, 12), 50.0),
                        _FYDividendObservation(date(2021, 3, 31), date(2021, 6, 1), invalid),
                    ],
                    asofs=[date(2021, 1, 31)],
                    horizons=(HORIZONS["1y"],),
                    eval_cap=date(2022, 1, 31),
                )[0]

                self.assertEqual(row.total_return_status, "unresolved_invalid_dividend")
                self.assertIsNone(row.realized_dividend_sum)
                self.assertIsNone(row.total_return)


class ForwardUnresolvedReasonTest(unittest.TestCase):
    """未解決の理由が、後段の分類が依存する形で観測に残ることを反証する。"""

    def test_entry_before_first_bar_leaves_entry_date_empty(self) -> None:
        # asof 時点で価格が 1 本も無い = 未上場。entry_date が空であることが根拠になる。
        rows = _ticker_forward_rows(
            "1000",
            [_bar(date(2025, 6, 30), 100.0)],
            asofs=[date(2025, 1, 31)],
            horizons=(HORIZONS["3m"],),
            eval_cap=date(2026, 6, 30),
        )
        row = rows[0]
        self.assertEqual(row.status, "unresolved_missing_entry")
        self.assertIsNone(row.entry_date)

    def test_entry_gap_after_earlier_pricing_keeps_the_earlier_entry_date(self) -> None:
        # 以前は価格が付いていたのに asof 近傍に無い = 取引可能名の取りこぼし候補。
        rows = _ticker_forward_rows(
            "1000",
            [_bar(date(2024, 10, 31), 100.0), _bar(date(2025, 6, 30), 120.0)],
            asofs=[date(2025, 1, 31)],
            horizons=(HORIZONS["3m"],),
            eval_cap=date(2026, 6, 30),
        )
        row = rows[0]
        self.assertEqual(row.status, "unresolved_missing_entry")
        self.assertEqual(row.entry_date, "2024-10-31")


class ForwardEntryToleranceTest(unittest.TestCase):
    def test_entry_resolves_from_a_bar_before_asof_when_asof_itself_did_not_trade(self) -> None:
        # 出来高の薄い銘柄は asof 当日に約定しないことがある。entry の許容が
        # 15 日あるのに asof 当日以降しか読まないと、前日に値が付いていても
        # 「entry なし」になり、その銘柄が計測から落ちる。
        with tempfile.TemporaryDirectory() as tmp:
            sqlite_path = Path(tmp) / "market.sqlite"
            conn = open_connection(sqlite_path)
            conn.commit()
            conn.close()
            asof = date(2025, 1, 31)
            # asof の 2 日前で系列を止め、asof 当日の bar を持たせない。
            insert_daily_bars_from_closes(
                sqlite_path, "1000", [100.0] * 40, end_date=asof - timedelta(days=2)
            )
            insert_daily_bars_from_closes(
                sqlite_path,
                "1000",
                [110.0] * 10,
                end_date=asof + timedelta(days=95),
            )
            insert_daily_bars_from_closes(
                sqlite_path, TOPIX_ETF_PROXY, [2000.0] * 200, end_date=asof + timedelta(days=95)
            )

            rows = compute_forward_returns(
                sqlite_path,
                asofs=[asof],
                tickers=["1000"],
                horizons=["3m"],
                control_event_exits={},
                failure_exits={},
            )

            row = next(row for row in rows if row.ticker == "1000")
            self.assertEqual(row.entry_date, (asof - timedelta(days=2)).isoformat())
            self.assertTrue(row.resolved)


class ControlEventExitTest(unittest.TestCase):
    """The offer price replaces only the windows the market could not close."""

    ASOF = date(2025, 1, 31)
    DELISTED_ON = date(2025, 3, 14)

    def _delisted_bars(self) -> list[JQuantsDailyBar]:
        return [
            _bar(self.ASOF, 100.0, factor=1.0),
            _bar(date(2025, 3, 13), 118.0, factor=1.0),
        ]

    def _rows(
        self,
        bars: list[JQuantsDailyBar],
        exits: tuple[ControlEventExit, ...],
        *,
        horizon: str = "3m",
    ) -> list[ForwardReturnRow]:
        return _ticker_forward_rows(
            "1000",
            bars,
            asofs=[self.ASOF],
            horizons=(HORIZONS[horizon],),
            eval_cap=date(2026, 6, 30),
            control_event_exits=exits,
        )

    def test_settled_offer_price_resolves_a_window_that_lost_its_exit(self) -> None:
        exits = (ControlEventExit(delisted_on=self.DELISTED_ON, offer_price_yen=120.0),)
        row = self._rows(self._delisted_bars(), exits)[0]
        self.assertTrue(row.resolved)
        self.assertEqual(row.status, "resolved_control_event_exit")
        self.assertEqual(row.exit_date, self.DELISTED_ON.isoformat())
        assert row.price_return is not None
        self.assertAlmostEqual(row.price_return, 0.2)

    def test_window_closed_by_the_market_keeps_its_observed_close(self) -> None:
        bars = [
            _bar(self.ASOF, 100.0, factor=1.0),
            _bar(date(2025, 4, 28), 118.0, factor=1.0),
        ]
        exits = (ControlEventExit(delisted_on=self.DELISTED_ON, offer_price_yen=999.0),)
        row = self._rows(bars, exits)[0]
        self.assertEqual(row.status, "resolved")
        assert row.price_return is not None
        self.assertAlmostEqual(row.price_return, 0.18)

    def test_an_offer_it_cannot_price_against_leaves_the_row_unresolved(self) -> None:
        # One reason per row why the offer cannot stand in for the missing exit. Each
        # has to bracket the window rather than produce a return, because a wrong
        # number here enters the measured distribution as a real observation.
        split_bars = [
            _bar(self.ASOF, 100.0, factor=1.0),
            _bar(date(2025, 3, 13), 118.0, factor=1.0),
            # The offer is quoted on the share basis of the delisting day while entry
            # closes are carried to the final bar's basis, so a split in between makes
            # the two incomparable.
            _bar(date(2025, 3, 21), 60.0, factor=0.5),
        ]
        cases: tuple[tuple[str, list[JQuantsDailyBar], tuple[ControlEventExit, ...]], ...] = (
            (
                "offer_outside_the_window",
                self._delisted_bars(),
                (ControlEventExit(delisted_on=date(2024, 12, 20), offer_price_yen=120.0),),
            ),
            (
                "two_offers_inside_one_window",
                self._delisted_bars(),
                (
                    ControlEventExit(delisted_on=self.DELISTED_ON, offer_price_yen=120.0),
                    ControlEventExit(delisted_on=date(2025, 3, 20), offer_price_yen=130.0),
                ),
            ),
            (
                "incomplete_adjustment_coverage",
                [_bar(self.ASOF, 100.0, factor=1.0), _bar(date(2025, 3, 13), 118.0)],
                (ControlEventExit(delisted_on=self.DELISTED_ON, offer_price_yen=120.0),),
            ),
            (
                "share_split_after_the_delisting",
                split_bars,
                (ControlEventExit(delisted_on=self.DELISTED_ON, offer_price_yen=120.0),),
            ),
        )

        for name, bars, exits in cases:
            with self.subTest(case=name):
                row = self._rows(bars, exits)[0]

                self.assertFalse(row.resolved)
                self.assertEqual(row.status, "unresolved_stale_exit")

    def test_read_control_event_exits_returns_every_offer_per_ticker(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            sqlite_path = Path(tmp) / "market.sqlite"
            connection = open_connection(sqlite_path)
            connection.executemany(
                "INSERT INTO tender_offer_exit_values("
                "ticker, delisted_on, offer_price_yen, offer_doc_id, result_doc_id, filed_on"
                ") VALUES (?, ?, ?, ?, ?, ?)",
                [
                    ("1000", "2025-03-14", 120.0, "S1", "S2", "2024-12-01"),
                    ("1000", "2020-03-14", 80.0, "S3", "S4", "2019-12-01"),
                ],
            )
            connection.commit()
            connection.close()

            exits = read_control_event_exits(sqlite_path)

            self.assertEqual(len(exits["1000"]), 2)
            self.assertEqual(sorted(item.offer_price_yen for item in exits["1000"]), [80.0, 120.0])


class AdjustmentFactorGuardTest(unittest.TestCase):
    """The factor guard has never fired on the stored bars, so it is exercised here.

    Every stored bar carries an adjustment factor, which makes `adjustment_factor_coverage`
    read `complete` on every row the store has ever produced. A check that has only ever
    been observed passing is not a check that has been shown to work: what it refuses has
    to be demonstrated, because the quantities behind it — a dividend carried to the final
    share basis, an offer price placed on the entry's basis — are wrong rather than absent
    when the factors are unknown.
    """

    ASOF = date(2020, 1, 31)

    def _dividends(self) -> list[_FYDividendObservation]:
        return [_FYDividendObservation(date(2020, 3, 31), date(2020, 5, 12), 50.0)]

    def _row(self, bars: list[JQuantsDailyBar]) -> ForwardReturnRow:
        return _ticker_forward_rows(
            "7203",
            bars,
            fy_dividends=self._dividends(),
            asofs=[self.ASOF],
            horizons=(HORIZONS["1y"],),
            eval_cap=date(2021, 6, 30),
        )[0]

    def test_bars_without_any_factor_refuse_the_total_return(self) -> None:
        row = self._row(
            [
                _bar(self.ASOF, 2500.0, ticker="7203"),
                _bar(date(2021, 1, 29), 3000.0, ticker="7203"),
            ]
        )
        self.assertEqual(row.adjustment_factor_coverage, "unknown")
        self.assertEqual(row.total_return_status, "unresolved_adjustment_factor")
        self.assertIsNone(row.total_return)
        self.assertIsNone(row.realized_dividend_sum)
        # The price move is still observed; only the per-share facts are refused.
        self.assertTrue(row.resolved)

    def test_one_bar_missing_its_factor_refuses_the_total_return(self) -> None:
        row = self._row(
            [
                _bar(self.ASOF, 2500.0, 1.0, ticker="7203"),
                _bar(date(2020, 6, 30), 2700.0, ticker="7203"),
                _bar(date(2021, 1, 29), 3000.0, ticker="7203"),
            ]
        )
        self.assertEqual(row.adjustment_factor_coverage, "incomplete")
        self.assertEqual(row.total_return_status, "unresolved_adjustment_factor")
        self.assertIsNone(row.total_return)

    def test_complete_factors_resolve_the_same_window(self) -> None:
        # The positive side of the same guard, so a refusal that fires on everything
        # cannot pass as a working check.
        row = self._row(
            [
                _bar(self.ASOF, 2500.0, 1.0, ticker="7203"),
                _bar(date(2020, 6, 30), 2700.0, 1.0, ticker="7203"),
                _bar(date(2021, 1, 29), 3000.0, 1.0, ticker="7203"),
            ]
        )
        self.assertEqual(row.adjustment_factor_coverage, "complete")
        self.assertEqual(row.total_return_status, "resolved")
        self.assertEqual(row.realized_dividend_sum, 50.0)

    def test_a_settled_offer_is_not_priced_when_the_factors_are_unknown(self) -> None:
        asof = date(2025, 1, 31)
        delisted_on = date(2025, 3, 14)
        rows = _ticker_forward_rows(
            "1000",
            [_bar(asof, 100.0), _bar(date(2025, 3, 13), 118.0)],
            asofs=[asof],
            horizons=(HORIZONS["3m"],),
            eval_cap=date(2026, 6, 30),
            control_event_exits=(ControlEventExit(delisted_on=delisted_on, offer_price_yen=120.0),),
        )
        row = rows[0]
        self.assertEqual(row.adjustment_factor_coverage, "unknown")
        self.assertNotEqual(row.status, "resolved_control_event_exit")
        self.assertFalse(row.resolved)


class FailureExitTest(unittest.TestCase):
    """A window ended by a failure delisting is a realized loss, not a missing observation.

    Dropping it removes the left tail from every measured return: at 3y the 516 windows
    that fall here have a median of -85.7% and a 20% win rate, against +36.6% mean for the
    windows that resolved. The capital is gone, so no reinvestment convention is needed to
    price it — the last close the market printed is what the position was worth.
    """

    ASOF = date(2025, 1, 31)
    DELISTED_ON = date(2025, 3, 14)

    def _rows(
        self,
        *,
        bars: list[JQuantsDailyBar] | None = None,
        failure_exits: tuple[FailureExit, ...] = (),
    ) -> list[ForwardReturnRow]:
        return _ticker_forward_rows(
            "1000",
            bars if bars is not None else [_bar(self.ASOF, 100.0), _bar(date(2025, 3, 13), 8.0)],
            asofs=[self.ASOF],
            horizons=(HORIZONS["3m"],),
            eval_cap=date(2026, 6, 30),
            failure_exits=failure_exits,
        )

    def _failure(self, reason: str = "上場維持基準への不適合") -> tuple[FailureExit, ...]:
        return (FailureExit(delisted_on=self.DELISTED_ON, reason=reason),)

    def test_a_failure_delisting_is_priced_at_the_last_close(self) -> None:
        row = self._rows(failure_exits=self._failure())[0]

        self.assertEqual(row.status, "resolved_failure_exit")
        self.assertTrue(row.resolved)
        self.assertAlmostEqual(row.price_return or 0.0, -0.92)
        self.assertEqual(row.exit_date, "2025-03-13")

    def test_a_delisting_it_cannot_price_against_leaves_the_row_unresolved(self) -> None:
        # The negative side of the same window, one reason per row, so a change that
        # fires on everything cannot pass as a working replacement.
        cases: tuple[tuple[str, list[JQuantsDailyBar] | None, tuple[FailureExit, ...]], ...] = (
            ("no_delisting_at_all", None, ()),
            (
                "delisting_outside_the_window",
                None,
                (FailureExit(delisted_on=date(2025, 8, 1), reason="破産手続き"),),
            ),
            (
                # A last close later than the removal is not the price trading stopped at.
                "traded_after_the_delisting",
                [_bar(self.ASOF, 100.0), _bar(date(2025, 3, 20), 8.0)],
                self._failure(),
            ),
            (
                "two_delistings_in_one_window",
                None,
                (
                    FailureExit(delisted_on=self.DELISTED_ON, reason="破産手続き"),
                    FailureExit(delisted_on=date(2025, 3, 20), reason="民事再生手続き"),
                ),
            ),
        )

        for name, bars, failure_exits in cases:
            with self.subTest(case=name):
                row = self._rows(bars=bars, failure_exits=failure_exits)[0]

                self.assertEqual(row.status, "unresolved_stale_exit")
                self.assertFalse(row.resolved)
                self.assertIsNone(row.price_return)

    def test_a_window_the_market_closed_is_untouched(self) -> None:
        """Realizing failures must not restate a single window the market itself closed."""

        row = self._rows(
            bars=[_bar(self.ASOF, 100.0), _bar(date(2025, 4, 28), 120.0)],
            failure_exits=self._failure(),
        )[0]

        self.assertEqual(row.status, "resolved")
        self.assertAlmostEqual(row.price_return or 0.0, 0.2)


class FailureReasonClassificationTest(unittest.TestCase):
    """JPX writes the reason as free prose, so both sides of the split are pinned.

    Reading an acquisition as a failure would book a takeover premium as a wipeout, which
    is worse than the omission being replaced. The rule is fail-closed accordingly.
    """

    def test_insolvency_and_listing_failures_are_failures(self) -> None:
        for reason in (
            "上場維持基準への不適合",
            "民事再生手続き",
            "破産手続き",
            "会社更生手続",
            "債務超過",
            "JASDAQ業績基準該当及び債務超過",
            "内部管理体制等の改善がなされず改善の見込みがなくなったと当取引所が認める場合",
            "有価証券報告書等の虚偽記載",
            "四半期報告書提出遅延",
            "時価総額が所要額未満",
            "新規上場申請に係る宣誓書における重大な違反",
            "公益・投資者保護（破産手続き開始の決定）",
        ):
            with self.subTest(reason=reason):
                self.assertTrue(is_failure_delisting(reason))

    def test_acquisitions_and_reorganisations_are_not_failures(self) -> None:
        for reason in (
            "株式の併合",
            "株式等売渡請求による取得",
            "他社による買収（公開買付け、株式併合）",
            "ＭＢＯ（公開買付け、株式併合）",
            "支配株主等による買収（株式併合）",
            "イオンの完全子会社化（株式交換）",
            "Ｊトラストに合併",
            "ＧＭＯＴＥＣＨホールディングスの完全子会社化（株式移転）",
            "申請による上場廃止",
        ):
            with self.subTest(reason=reason):
                self.assertFalse(is_failure_delisting(reason))

    def test_a_reverse_split_that_wiped_the_equity_out_is_a_failure(self) -> None:
        """`株式の併合` alone is a squeeze-out; next to an insolvency it is the wipeout."""

        self.assertTrue(
            is_failure_delisting(
                "株式の併合・破産手続、再生手続又は更生手続に準ずる状態（債務免除）"
            )
        )

    def test_an_insolvency_phrase_inside_an_acquisition_does_not_realize_it(self) -> None:
        """The fail-closed direction: an acquisition wins, and the row stays unresolved."""

        self.assertFalse(is_failure_delisting("民事再生手続き中の会社の完全子会社化"))

    def test_read_failure_exits_keeps_only_the_failures(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            sqlite_path = Path(tmp) / "market.sqlite"
            connection = open_connection(sqlite_path)
            connection.executemany(
                "INSERT INTO jpx_delistings(delisted_on, ticker, name, market, reason) "
                "VALUES (?, ?, ?, ?, ?)",
                [
                    ("2025-03-14", "1000", "A", "プライム", "上場維持基準への不適合"),
                    ("2020-03-14", "1000", "A", "プライム", "民事再生手続き"),
                    ("2025-05-01", "2000", "B", "スタンダード", "株式の併合"),
                ],
            )
            connection.commit()
            connection.close()

            exits = read_failure_exits(sqlite_path)

            self.assertEqual(sorted(exits), ["1000"])
            self.assertEqual(len(exits["1000"]), 2)


if __name__ == "__main__":
    unittest.main()
