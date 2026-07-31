from __future__ import annotations

import unittest
from datetime import date

from baibai_engine.macro.indicators.providers.option_iv import (
    CALL,
    PUT,
    OptionQuote,
    fear_readings,
    quotes_from_records,
)

ASOF = date(2026, 7, 29)
NEAR = date(2026, 8, 14)  # 16 days out
FAR = date(2026, 9, 11)  # 44 days out


def _chain(
    expiry: date,
    *,
    underlying: float = 40_000.0,
    atm_put: float = 20.0,
    atm_call: float = 22.0,
    wing_put: float = 30.0,
    wing_call: float = 18.0,
) -> list[OptionQuote]:
    def quote(side: str, strike: float, vol: float) -> OptionQuote:
        return OptionQuote(
            put_call=side,
            strike=strike,
            expiry=expiry,
            implied_volatility=vol,
            underlying=underlying,
        )

    return [
        quote(PUT, underlying, atm_put),
        quote(CALL, underlying, atm_call),
        quote(PUT, underlying * 0.95, wing_put),
        quote(CALL, underlying * 1.05, wing_call),
    ]


class FearReadingsTest(unittest.TestCase):
    def test_the_atm_reading_averages_the_put_and_the_call(self) -> None:
        # Each side carries its own directional bias, so the average is closer to
        # what the market charges for movement rather than for direction.
        readings = fear_readings([*_chain(NEAR), *_chain(FAR)], ASOF)

        self.assertIsNotNone(readings.iv_30d)
        assert readings.iv_30d is not None
        self.assertAlmostEqual(readings.iv_30d, 21.0, places=6)

    def test_the_skew_is_the_downside_wing_minus_the_upside_wing(self) -> None:
        readings = fear_readings([*_chain(NEAR), *_chain(FAR)], ASOF)

        self.assertEqual(readings.iv_skew, 12.0)

    def test_a_negative_skew_is_reported_rather_than_clamped(self) -> None:
        # Upside vol above downside vol is an unusual but real state; reporting it
        # as zero would hide the very asymmetry the reading exists to show.
        chain = _chain(NEAR, wing_put=15.0, wing_call=19.0)
        readings = fear_readings([*chain, *_chain(FAR)], ASOF)

        self.assertEqual(readings.iv_skew, -4.0)

    def test_the_term_reading_is_the_second_month_minus_the_first(self) -> None:
        readings = fear_readings([*_chain(NEAR), *_chain(FAR, atm_put=30.0, atm_call=30.0)], ASOF)

        self.assertEqual(readings.iv_term, 9.0)

    def test_the_constant_maturity_interpolates_in_variance(self) -> None:
        # Interpolating volatility directly would understate the reading whenever
        # the two expiries disagree, which is exactly when the market is scared.
        readings = fear_readings(
            [*_chain(NEAR, atm_put=20.0, atm_call=20.0), *_chain(FAR, atm_put=40.0, atm_call=40.0)],
            ASOF,
        )

        assert readings.iv_30d is not None
        weight = (44 - 30) / (44 - 16)
        expected = (weight * 20.0**2 + (1 - weight) * 40.0**2) ** 0.5
        self.assertAlmostEqual(readings.iv_30d, expected, places=9)
        self.assertGreater(readings.iv_30d, 30.0)  # above the volatility midpoint

    def test_an_expiry_inside_its_last_week_is_not_used(self) -> None:
        # A contract days from settlement prices the SQ auction, not the month.
        readings = fear_readings([*_chain(date(2026, 8, 3)), *_chain(NEAR), *_chain(FAR)], ASOF)

        self.assertEqual(readings.iv_term, 0.0)

    def test_a_chain_that_does_not_straddle_the_target_declines_the_maturity(self) -> None:
        readings = fear_readings(_chain(NEAR), ASOF)

        self.assertIsNone(readings.iv_30d)
        self.assertIsNone(readings.iv_term)
        self.assertEqual(readings.iv_skew, 12.0)

    def test_an_empty_chain_reads_nothing(self) -> None:
        self.assertEqual(fear_readings([], ASOF), fear_readings([], ASOF))
        self.assertIsNone(fear_readings([], ASOF).iv_30d)


class QuoteParsingTest(unittest.TestCase):
    def _record(self, **overrides: object) -> dict[str, object]:
        record: dict[str, object] = {
            "SQD": "2026-08-14",
            "Strike": 40000.0,
            "IV": 20.0,
            "UnderPx": 40000.0,
            "PCDiv": "1",
        }
        record.update(overrides)
        return record

    def test_a_row_missing_a_field_is_dropped_rather_than_failing(self) -> None:
        # A chain always carries contracts with no quote; a day is still readable.
        rows = [self._record(), self._record(IV=None), self._record(Strike="")]

        self.assertEqual(len(quotes_from_records(rows, ASOF)), 1)

    def test_an_unknown_side_is_dropped(self) -> None:
        self.assertEqual(quotes_from_records([self._record(PCDiv="9")], ASOF), [])


if __name__ == "__main__":
    unittest.main()
