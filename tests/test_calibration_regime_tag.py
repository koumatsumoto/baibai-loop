from __future__ import annotations

import unittest
from datetime import date, timedelta

from baibai_engine.screening.calibration.regime_tag import (
    DRAWDOWN_WINDOW_BARS,
    regime_at,
)

START = date(2020, 1, 1)


def _series(values: list[float]) -> list[tuple[date, float]]:
    # Consecutive days stand in for trading days; the windows count bars, not
    # calendar distance, so the spacing does not enter the calculation.
    return [(START + timedelta(days=index), value) for index, value in enumerate(values)]


def _asof(values: list[float]) -> date:
    return START + timedelta(days=len(values) - 1)


class RegimeAtTest(unittest.TestCase):
    def test_a_deep_drawdown_is_stress(self) -> None:
        values = [100.0] * DRAWDOWN_WINDOW_BARS + [80.0]
        reading = regime_at(_series(values), _asof(values))

        self.assertEqual(reading.regime, "stress")
        assert reading.drawdown is not None
        self.assertAlmostEqual(reading.drawdown, -0.2)

    def test_a_moderate_drawdown_that_is_rising_is_recovery(self) -> None:
        # Down 10% from the peak but the last 60 bars are up: the definition's
        # second clause, which stress would otherwise absorb.
        values = [100.0] * DRAWDOWN_WINDOW_BARS + [85.0] * 60 + [90.0]
        reading = regime_at(_series(values), _asof(values))

        self.assertEqual(reading.regime, "recovery")

    def test_a_moderate_drawdown_that_is_still_falling_is_not_recovery(self) -> None:
        values = [100.0] * DRAWDOWN_WINDOW_BARS + [95.0] * 60 + [91.0]
        reading = regime_at(_series(values), _asof(values))

        self.assertEqual(reading.regime, "normal")

    def test_a_quiet_market_near_its_high_is_extended(self) -> None:
        # Turbulent for years, then calm and back at the high: today's volatility
        # sits low inside its own five-year range, which is what `extended` names.
        # The calm stretch must be longer than the 252-bar drawdown window, or the
        # turbulent peak is still inside it and the drawdown clause blocks the state.
        turbulent = [100.0 + (6.0 if index % 2 else -6.0) for index in range(900)]
        calm = [100.0 + (0.05 if index % 2 else -0.05) for index in range(350)]
        values = [*turbulent, *calm, 100.05]
        reading = regime_at(_series(values), _asof(values))

        assert reading.volatility_percentile is not None
        self.assertLess(reading.volatility_percentile, 0.5)
        self.assertEqual(reading.regime, "extended")

    def test_too_little_history_is_unknown_rather_than_normal(self) -> None:
        # A cohort whose regime cannot be read has to stay visible as such; folding
        # it into `normal` would put unlabelled cohorts inside a measured group.
        values = [100.0] * 10
        reading = regime_at(_series(values), _asof(values))

        self.assertEqual(reading.regime, "unknown")
        self.assertIsNone(reading.drawdown)

    def test_the_label_reads_only_closes_at_or_before_the_asof(self) -> None:
        # The series continues past the as-of with a crash. A label that used it
        # would be reading the future into the cohort.
        values = [100.0] * DRAWDOWN_WINDOW_BARS + [100.0]
        asof = _asof(values)
        with_future = _series([*values, 50.0, 40.0])

        self.assertEqual(
            regime_at(with_future, asof).regime, regime_at(_series(values), asof).regime
        )

    def test_an_empty_series_is_unknown(self) -> None:
        self.assertEqual(regime_at([], date(2026, 6, 30)).regime, "unknown")


if __name__ == "__main__":
    unittest.main()
