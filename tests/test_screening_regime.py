from __future__ import annotations

import sqlite3
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

from baibai_loop.screening.regime import (
    MarketRegime,
    classify_market_regime,
    compute_market_regime,
)
from tests.helpers.screening_sqlite import insert_daily_bars_from_closes


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


class ClassifyMarketRegimeTests(unittest.TestCase):
    def test_classify_rally_at_trend_threshold(self) -> None:
        self.assertIs(classify_market_regime(0.03), MarketRegime.RISK_ON_RALLY)

    def test_classify_selloff_at_trend_threshold(self) -> None:
        self.assertIs(classify_market_regime(-0.03), MarketRegime.RISK_OFF_SELLOFF)

    def test_classify_neutral_inside_trend_band(self) -> None:
        self.assertIs(classify_market_regime(0.029), MarketRegime.NEUTRAL_RANGE)
        self.assertIs(classify_market_regime(-0.029), MarketRegime.NEUTRAL_RANGE)

    def test_classify_unknown_when_trend_missing(self) -> None:
        self.assertIs(classify_market_regime(None), MarketRegime.UNKNOWN)


class ComputeMarketRegimeTests(unittest.TestCase):
    def test_compute_returns_none_when_sqlite_missing(self) -> None:
        self.assertIsNone(
            compute_market_regime(Path("/nonexistent/market.sqlite"), date(2026, 5, 29))
        )

    def test_compute_classifies_rally_from_bars(self) -> None:
        asof = date(2026, 5, 29)
        with tempfile.TemporaryDirectory() as tmpdir:
            sqlite_path = Path(tmpdir) / "market.sqlite"
            # Benchmark up ~5% over the last 20 bars.
            _insert_bars(sqlite_path, "1321", _rising_closes(25, start=100.0, step=0.25), end=asof)
            snapshot = compute_market_regime(sqlite_path, asof)
            assert snapshot is not None
            self.assertEqual(snapshot.asof, asof)
            self.assertEqual(snapshot.eval_date, asof)
            assert snapshot.benchmark_return_20d is not None
            self.assertGreater(snapshot.benchmark_return_20d, 0.03)
            self.assertIs(snapshot.regime, MarketRegime.RISK_ON_RALLY)

    def test_compute_degrades_to_unknown_when_history_too_short(self) -> None:
        asof = date(2026, 5, 29)
        with tempfile.TemporaryDirectory() as tmpdir:
            sqlite_path = Path(tmpdir) / "market.sqlite"
            _insert_bars(sqlite_path, "1321", _rising_closes(5, start=100.0, step=1.0), end=asof)
            snapshot = compute_market_regime(sqlite_path, asof)
            assert snapshot is not None
            self.assertIsNone(snapshot.benchmark_return_20d)
            self.assertIs(snapshot.regime, MarketRegime.UNKNOWN)

    def test_compute_ignores_bars_after_asof(self) -> None:
        asof = date(2026, 5, 29)
        with tempfile.TemporaryDirectory() as tmpdir:
            sqlite_path = Path(tmpdir) / "market.sqlite"
            _insert_bars(sqlite_path, "1321", _rising_closes(25, start=100.0, step=0.25), end=asof)
            # A later crash bar must not leak into the asof evaluation.
            conn = sqlite3.connect(sqlite_path)
            try:
                conn.execute(
                    "INSERT INTO jquants_daily_bars(ticker, traded_at, close, adjustment_close)"
                    " VALUES ('1321', ?, 1.0, 1.0)",
                    ((asof + timedelta(days=3)).isoformat(),),
                )
                conn.commit()
            finally:
                conn.close()
            snapshot = compute_market_regime(sqlite_path, asof)
            assert snapshot is not None
            self.assertEqual(snapshot.eval_date, asof)
            assert snapshot.benchmark_return_20d is not None
            self.assertGreater(snapshot.benchmark_return_20d, 0.0)


if __name__ == "__main__":
    unittest.main()
