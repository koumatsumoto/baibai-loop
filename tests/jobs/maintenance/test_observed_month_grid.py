import unittest
from datetime import date

from baibai_jobs.maintenance.market.grid import observed_month_end_dates


class MonthEndDatesTest(unittest.TestCase):
    def test_sparse_dates_are_not_guaranteed_calendar_month_ends(self) -> None:
        self.assertEqual(
            observed_month_end_dates(
                [(date(2025, 1, 31), 4100), (date(2025, 2, 13), 4000), (date(2025, 3, 3), 4100)],
                end=date(2025, 2, 14),
                min_tickers=2000,
            ),
            [date(2025, 1, 31), date(2025, 2, 13)],
        )

    def test_accepts_last_trading_date_before_weekend_month_end(self) -> None:
        observed = [(date(2026, 1, 30), 4100), (date(2026, 2, 2), 4100)]
        for cutoff in (date(2026, 1, 30), date(2026, 1, 31)):
            with self.subTest(cutoff=cutoff):
                self.assertEqual(
                    observed_month_end_dates(observed, end=cutoff, min_tickers=2000),
                    [date(2026, 1, 30)],
                )

    def test_observed_month_end_dates_picks_last_qualifying_day_per_month(self) -> None:
        day_counts = [
            (date(2025, 1, 30), 4000),
            (date(2025, 1, 31), 4100),
            (date(2025, 2, 27), 4000),
            # 部分データ日 (取込途中断面) は月末営業日として選ばない。
            (date(2025, 2, 28), 100),
            # 後続月の観測があるため1-2月を月次標本に選べる。
            # 全営業日の取得完了を保証するものではなく、最終data月の3月は選ばない。
            (date(2025, 3, 3), 4100),
        ]
        self.assertEqual(
            observed_month_end_dates(day_counts, end=date(2025, 3, 31), min_tickers=2000),
            [date(2025, 1, 31), date(2025, 2, 27)],
        )

    def test_observed_month_end_dates_excludes_a_known_later_date_after_cutoff(self) -> None:
        # 同月の後続観測2/27が既知なので、end以前の2/13へ切り詰めて採用しない。
        day_counts = [
            (date(2025, 1, 31), 4100),
            (date(2025, 2, 13), 4000),
            (date(2025, 2, 27), 4000),
            (date(2025, 3, 3), 4100),
        ]
        self.assertEqual(
            observed_month_end_dates(day_counts, end=date(2025, 2, 14), min_tickers=2000),
            [date(2025, 1, 31)],
        )

    def test_observed_month_end_dates_empty_when_all_below_threshold(self) -> None:
        self.assertEqual(
            observed_month_end_dates(
                [(date(2025, 1, 31), 10)], end=date(2025, 1, 31), min_tickers=2000
            ),
            [],
        )
