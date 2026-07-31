from __future__ import annotations

import unittest
from datetime import date, timedelta

from tools.research.topix_regime import (
    DRAWDOWN_WINDOW_BARS,
    _classify,
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

    def test_each_pre_registered_threshold_sits_where_the_report_put_it(self) -> None:
        # The report says the thresholds and the branch order are part of the
        # definition and are not to be moved. Fixtures built from the constants
        # would pass for any constant, so the boundaries are written out.
        def label(drawdown_pct: float, up: bool) -> str:
            # `up` controls the 60-bar return only: the close 61 bars back sits
            # below the last close when rising and above it when falling, while the
            # peak stays at 100 so the drawdown is exactly `drawdown_pct`.
            last = 100.0 * (1 + drawdown_pct)
            start = last * (0.98 if up else 1.02)
            values = [100.0] * DRAWDOWN_WINDOW_BARS + [start] + [last] * 60
            return regime_at(_series(values), _asof(values)).regime

        # The comparisons sit either side of each threshold rather than exactly on
        # it: a close constructed to land on -0.15 lands a rounding step off, so an
        # exact-boundary assertion would measure floating point, not the rule.
        self.assertEqual(label(-0.16, up=False), "stress")
        self.assertNotEqual(label(-0.14, up=False), "stress")
        # recovery needs both the depth and a rising 60 bars
        self.assertEqual(label(-0.09, up=True), "recovery")
        self.assertNotEqual(label(-0.07, up=True), "recovery")
        self.assertNotEqual(label(-0.09, up=False), "recovery")

    def test_stress_is_evaluated_before_recovery(self) -> None:
        # A deep drawdown that is also rising satisfies both clauses. The report
        # fixes the order, so it must read as stress.
        values = [100.0] * DRAWDOWN_WINDOW_BARS + [79.0] * 60 + [84.0]
        self.assertEqual(regime_at(_series(values), _asof(values)).regime, "stress")

    def test_the_drawdown_window_is_252_bars(self) -> None:
        # Written with literals, not with the constant: a fixture built from
        # DRAWDOWN_WINDOW_BARS moves with it and passes for any window length.
        # 251 highs then a low keeps the peak inside a 252-bar window; one high
        # followed by 252 lows pushes it out.
        inside = [100.0] * 251 + [80.0]
        outside = [100.0] + [80.0] * 252

        self.assertEqual(regime_at(_series(inside), _asof(inside)).regime, "stress")
        self.assertNotEqual(regime_at(_series(outside), _asof(outside)).regime, "stress")

    def test_the_extended_thresholds_sit_where_the_report_put_them(self) -> None:
        # `_classify` is called directly so the comparison is tested rather than
        # the arithmetic that feeds it — a close constructed to land exactly on a
        # threshold measures floating point instead of the rule.
        self.assertEqual(_classify(-0.02, 0.0, 0.4), "extended")
        self.assertEqual(_classify(-0.04, 0.0, 0.4), "normal")
        self.assertEqual(_classify(-0.02, 0.0, 0.6), "normal")


if __name__ == "__main__":
    unittest.main()
