from __future__ import annotations

import sys
import unittest
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from baibai_loop.screening.calibration.forward import (
    HORIZONS,
    _ticker_forward_rows,
)
from baibai_loop.screening.calibration.grid import complete_month_end_dates
from baibai_loop.screening.providers.jquants import JQuantsDailyBar


def _bar(day: date, close: float, factor: float | None = None) -> JQuantsDailyBar:
    return JQuantsDailyBar(
        ticker="1000",
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
            _bar(date(2025, 8, 1), 150.0),
        ]
        rows = _ticker_forward_rows(
            "1000",
            bars,
            asofs=[date(2025, 1, 31)],
            horizons={"3m": 91, "6m": 182},
            eval_cap=date(2025, 8, 1),
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
            horizons={"3m": 91},
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
            horizons={"12m": 365},
            eval_cap=date(2025, 3, 31),
        )
        self.assertFalse(rows[0].resolved)
        self.assertIsNone(rows[0].price_return)

    def test_forward_return_flags_stale_exit_price(self) -> None:
        # 上場廃止・長期停止: target 直近の bar が 15 日超古い → stale flag。
        bars = [_bar(date(2025, 1, 31), 100.0), _bar(date(2025, 2, 28), 130.0)]
        rows = _ticker_forward_rows(
            "1000",
            bars,
            asofs=[date(2025, 1, 31)],
            horizons={"3m": 91},
            eval_cap=date(2025, 6, 30),
        )
        row = rows[0]
        self.assertTrue(row.resolved)
        self.assertTrue(row.stale_price)
        assert row.price_return is not None
        self.assertAlmostEqual(row.price_return, 0.3)

    def test_forward_return_invalid_entry_when_no_recent_bar_at_asof(self) -> None:
        # asof より 15 日超前の bar しか無い (上場前 / 廃止後) → 全 horizon 未解決。
        bars = [_bar(date(2024, 10, 31), 100.0)]
        rows = _ticker_forward_rows(
            "1000",
            bars,
            asofs=[date(2025, 1, 31)],
            horizons=dict(HORIZONS),
            eval_cap=date(2026, 6, 30),
        )
        self.assertEqual(len(rows), len(HORIZONS))
        self.assertTrue(all(not row.resolved for row in rows))


if __name__ == "__main__":
    unittest.main()
