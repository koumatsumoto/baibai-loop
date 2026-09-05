from __future__ import annotations

import sqlite3
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

from tests.helpers.screening_sqlite import insert_daily_bars_from_closes

from baibai_engine.screening.benchmark_trend import (
    BenchmarkTrendState,
    classify_benchmark_trend,
    compute_benchmark_trend,
)


def _insert_bars(
    sqlite_path: Path,
    ticker: str,
    closes: list[float],
    *,
    end: date,
) -> None:
    insert_daily_bars_from_closes(sqlite_path, ticker, closes, end_date=end)


def _rising_closes(count: int, *, start: float, step: float) -> list[float]:
    return [start + step * index for index in range(count)]


class ClassifyBenchmarkTrendStateTests(unittest.TestCase):
    def test_classify_rally_at_trend_threshold(self) -> None:
        self.assertIs(classify_benchmark_trend(0.03), BenchmarkTrendState.UPTREND)

    def test_classify_selloff_at_trend_threshold(self) -> None:
        self.assertIs(classify_benchmark_trend(-0.03), BenchmarkTrendState.DOWNTREND)

    def test_classify_neutral_inside_trend_band(self) -> None:
        self.assertIs(classify_benchmark_trend(0.029), BenchmarkTrendState.NEUTRAL)
        self.assertIs(classify_benchmark_trend(-0.029), BenchmarkTrendState.NEUTRAL)

    def test_classify_unknown_when_trend_missing(self) -> None:
        self.assertIs(classify_benchmark_trend(None), BenchmarkTrendState.UNKNOWN)


class ComputeBenchmarkTrendStateTests(unittest.TestCase):
    def test_compute_returns_none_when_sqlite_missing(self) -> None:
        self.assertIsNone(
            compute_benchmark_trend(Path("/nonexistent/market.sqlite"), date(2026, 5, 29))
        )

    def test_compute_classifies_rally_from_bars(self) -> None:
        as_of = date(2026, 5, 29)
        with tempfile.TemporaryDirectory() as tmpdir:
            sqlite_path = Path(tmpdir) / "market.sqlite"
            # Benchmark up ~5% over the last 20 bars.
            _insert_bars(sqlite_path, "1321", _rising_closes(25, start=100.0, step=0.25), end=as_of)
            snapshot = compute_benchmark_trend(sqlite_path, as_of)
            assert snapshot is not None
            self.assertEqual(snapshot.as_of, as_of)
            self.assertEqual(snapshot.observation_date, as_of)
            assert snapshot.benchmark_return_20d is not None
            self.assertGreater(snapshot.benchmark_return_20d, 0.03)
            self.assertIs(snapshot.trend_state, BenchmarkTrendState.UPTREND)
            # Monotonic rally: the latest close is the window high, so the gap
            # from the high is 0.0 — the "最高値圏" signal that should make RR
            # judgement stress the downside.
            self.assertEqual(snapshot.benchmark_gap_from_high, 0.0)

    def test_compute_gap_from_high_negative_after_pullback(self) -> None:
        as_of = date(2026, 5, 29)
        with tempfile.TemporaryDirectory() as tmpdir:
            sqlite_path = Path(tmpdir) / "market.sqlite"
            # Rally to a peak (~110), then pull back to 104: window high stays
            # 110 so the gap from high is negative even though the trend is up.
            closes = [*_rising_closes(20, start=100.0, step=0.5), 109.0, 108.0, 106.0, 105.0, 104.0]
            _insert_bars(sqlite_path, "1321", closes, end=as_of)
            snapshot = compute_benchmark_trend(sqlite_path, as_of)
            assert snapshot is not None
            assert snapshot.benchmark_gap_from_high is not None
            self.assertLess(snapshot.benchmark_gap_from_high, 0.0)
            self.assertAlmostEqual(snapshot.benchmark_gap_from_high, 104.0 / 109.5 - 1, places=6)

    def test_compute_gap_from_high_uses_full_52w_window_not_recent_months(self) -> None:
        as_of = date(2026, 5, 29)
        with tempfile.TemporaryDirectory() as tmpdir:
            sqlite_path = Path(tmpdir) / "market.sqlite"
            # Peak (200) ~210 bars ago, then a long decline to 147.75. A short
            # few-month window would miss the earlier peak and understate the gap;
            # the 52-week window must anchor on 200.
            closes = [
                *_rising_closes(50, start=100.0, step=2.0),
                *[200.0 - 0.25 * i for i in range(210)],
            ]
            _insert_bars(sqlite_path, "1321", closes, end=as_of)
            snapshot = compute_benchmark_trend(sqlite_path, as_of)
            assert snapshot is not None
            assert snapshot.benchmark_gap_from_high is not None
            self.assertAlmostEqual(snapshot.benchmark_gap_from_high, 147.75 / 200.0 - 1, places=6)

    def test_compute_degrades_to_unknown_when_history_too_short(self) -> None:
        as_of = date(2026, 5, 29)
        with tempfile.TemporaryDirectory() as tmpdir:
            sqlite_path = Path(tmpdir) / "market.sqlite"
            _insert_bars(sqlite_path, "1321", _rising_closes(5, start=100.0, step=1.0), end=as_of)
            snapshot = compute_benchmark_trend(sqlite_path, as_of)
            assert snapshot is not None
            self.assertIsNone(snapshot.benchmark_return_20d)
            self.assertIs(snapshot.trend_state, BenchmarkTrendState.UNKNOWN)

    def test_compute_ignores_bars_after_asof(self) -> None:
        as_of = date(2026, 5, 29)
        with tempfile.TemporaryDirectory() as tmpdir:
            sqlite_path = Path(tmpdir) / "market.sqlite"
            _insert_bars(sqlite_path, "1321", _rising_closes(25, start=100.0, step=0.25), end=as_of)
            # A later crash bar must not leak into the as_of evaluation.
            conn = sqlite3.connect(sqlite_path)
            try:
                conn.execute(
                    "INSERT INTO jquants_daily_bars(ticker, traded_at, close, adjustment_close)"
                    " VALUES ('1321', ?, 1.0, 1.0)",
                    ((as_of + timedelta(days=3)).isoformat(),),
                )
                conn.commit()
            finally:
                conn.close()
            snapshot = compute_benchmark_trend(sqlite_path, as_of)
            assert snapshot is not None
            self.assertEqual(snapshot.observation_date, as_of)
            assert snapshot.benchmark_return_20d is not None
            self.assertGreater(snapshot.benchmark_return_20d, 0.0)

    def test_compute_keeps_adjustment_event_when_event_day_has_no_close(self) -> None:
        as_of = date(2026, 5, 29)
        with tempfile.TemporaryDirectory() as tmpdir:
            sqlite_path = Path(tmpdir) / "market.sqlite"
            # A 1:2 split turns the raw 100 close into 50.  The ex-rights session is
            # suspended, so its event row legitimately has no close.
            _insert_bars(sqlite_path, "1321", [100.0] * 20 + [50.0] * 5, end=as_of)
            event_day = as_of - timedelta(days=4)
            conn = sqlite3.connect(sqlite_path)
            try:
                conn.execute(
                    "UPDATE jquants_daily_bars SET close = NULL, adjustment_close = NULL, "
                    "adjustment_factor = 0.5 WHERE ticker = '1321' AND traded_at = ?",
                    (event_day.isoformat(),),
                )
                conn.commit()
            finally:
                conn.close()

            snapshot = compute_benchmark_trend(sqlite_path, as_of)

            assert snapshot is not None
            self.assertAlmostEqual(snapshot.benchmark_return_20d or 0.0, 0.0)
            self.assertIs(snapshot.trend_state, BenchmarkTrendState.NEUTRAL)


if __name__ == "__main__":
    unittest.main()
