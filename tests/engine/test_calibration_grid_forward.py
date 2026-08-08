from __future__ import annotations

import sys
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from tests.helpers.screening_sqlite import insert_daily_bars_from_closes

from baibai_engine.market.benchmark import TOPIX_ETF_PROXY
from baibai_engine.screening.calibration.forward import (
    HORIZONS,
    _FYDividendObservation,
    _ticker_forward_rows,
    compute_forward_returns,
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
                sqlite_path, asofs=[asof], tickers=["1000"], horizons=["3m"]
            )

            row = next(row for row in rows if row.ticker == "1000")
            self.assertEqual(row.entry_date, (asof - timedelta(days=2)).isoformat())
            self.assertTrue(row.resolved)


if __name__ == "__main__":
    unittest.main()
