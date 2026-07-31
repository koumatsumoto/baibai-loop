from __future__ import annotations

import unittest
from datetime import date, datetime
from zoneinfo import ZoneInfo

from baibai_engine.macro.indicators.providers.option_iv import (
    CALL,
    PUT,
    OptionQuote,
    fear_readings,
    quotes_from_records,
)

ASOF = date(2026, 7, 29)
NEAR = date(2026, 8, 14)  # 16 days out
MID = date(2026, 8, 23)  # 25 days out
FAR = date(2026, 9, 11)  # 44 days out
UNDERLYING = 40_000.0

# A ladder wide enough that 0.90 / 0.95 / 1.00 / 1.05 / 1.10 moneyness each land on
# their own strike carrying their own volatility, so a reading that picks the wrong
# strike returns a different number instead of the same one.
_LADDER: dict[float, float] = {
    0.90: 34.0,
    0.95: 30.0,
    0.975: 25.0,
    1.00: 20.0,
    1.025: 18.0,
    1.05: 17.0,
    1.10: 15.0,
}


def _chain(
    expiry: date,
    *,
    underlying: float = UNDERLYING,
    level: float = 0.0,
    basis_gap: float = 0.0,
    gaps: dict[float, float] | None = None,
    ladder: dict[float, float] | None = None,
) -> list[OptionQuote]:
    """One expiry's quotes.

    `level` shifts the whole smile, so a test can tell two expiries apart. The gap
    lowers the call at a strike, reproducing a source that priced the two sides
    against different underlyings: the put-minus-call gap is the gap, while the
    shape of either side alone is untouched. `gaps` sets it per moneyness so a test
    can put the disagreement where it wants it; `basis_gap` applies one everywhere.
    """
    rungs = _LADDER if ladder is None else ladder
    quotes: list[OptionQuote] = []
    for moneyness, volatility in rungs.items():
        strike = underlying * moneyness
        gap = basis_gap if gaps is None else gaps.get(moneyness, 0.0)
        for side, vol in ((PUT, volatility + level), (CALL, volatility + level - gap)):
            quotes.append(
                OptionQuote(
                    put_call=side,
                    strike=strike,
                    expiry=expiry,
                    implied_volatility=vol,
                    underlying=underlying,
                )
            )
    return quotes


class FearReadingsTest(unittest.TestCase):
    def test_the_atm_reading_averages_the_put_and_the_call(self) -> None:
        # Each side carries its own directional bias, so the average is closer to
        # what the market charges for movement rather than for direction.
        readings = fear_readings([*_chain(NEAR, basis_gap=4.0), *_chain(FAR, basis_gap=4.0)], ASOF)

        assert readings.iv_30d is not None
        self.assertAlmostEqual(readings.iv_30d, 18.0, places=6)

    def test_the_skew_is_the_downside_put_minus_the_at_the_money_put(self) -> None:
        readings = fear_readings([*_chain(NEAR), *_chain(FAR)], ASOF)

        self.assertEqual(readings.iv_skew, 10.0)

    def test_the_skew_ignores_a_basis_error_the_source_left_in_the_chain(self) -> None:
        # A day where the source priced puts and calls against different underlyings
        # must not move the asymmetry: both legs come from the same side.
        clean = fear_readings([*_chain(NEAR), *_chain(FAR)], ASOF)
        skewed = fear_readings([*_chain(NEAR, basis_gap=12.0), *_chain(FAR, basis_gap=12.0)], ASOF)

        self.assertEqual(skewed.iv_skew, clean.iv_skew)

    def test_sides_that_disagree_beyond_the_bound_withhold_the_differences(self) -> None:
        # Past the bound the chain cannot be trusted to subtract one volatility from
        # another, but the level still averages the error away and is kept.
        readings = fear_readings(
            [*_chain(NEAR, basis_gap=20.0), *_chain(FAR, basis_gap=20.0)], ASOF
        )

        self.assertIsNone(readings.iv_skew)
        self.assertIsNone(readings.iv_term)
        self.assertIsNotNone(readings.iv_30d)

    def test_the_basis_check_reads_the_strikes_around_the_money_not_only_the_money(self) -> None:
        # A source can agree at one strike and disagree either side of it, so a check
        # that only looked at the money would pass a chain it should refuse.
        ladder = {0.90: 50.0, 0.95: 46.0, 0.975: 42.0, 1.00: 40.0, 1.025: 38.0, 1.05: 36.0}
        around = {0.95: 25.0, 0.975: 25.0, 1.025: 25.0, 1.05: 25.0}

        readings = fear_readings(
            [
                *_chain(NEAR, ladder=ladder, gaps=around),
                *_chain(FAR, ladder=ladder, gaps=around),
            ],
            ASOF,
        )

        self.assertIsNone(readings.iv_skew)

    def test_the_basis_check_stays_out_of_the_wings(self) -> None:
        # Far out of the money a thin quote moves the volatility by points for reasons
        # that are not the basis, so a wider band would refuse sound chains. Here the
        # wings outnumber the strikes around the money, so a band that reached them
        # would take its verdict from them.
        ladder = {
            0.80: 60.0,
            0.85: 55.0,
            0.95: 30.0,
            1.00: 20.0,
            1.05: 17.0,
            1.15: 40.0,
            1.20: 45.0,
        }
        wings = {0.80: 25.0, 0.85: 25.0, 1.15: 25.0, 1.20: 25.0}

        readings = fear_readings(
            [
                *_chain(NEAR, ladder=ladder, gaps=wings),
                *_chain(FAR, ladder=ladder, gaps=wings),
            ],
            ASOF,
        )

        self.assertEqual(readings.iv_skew, 10.0)

    def test_a_chain_quoting_only_puts_withholds_the_differences(self) -> None:
        # With nothing to compare the sides against, the basis is unknown rather
        # than sound.
        puts = [quote for quote in _chain(NEAR) if quote.put_call == PUT]
        far = [quote for quote in _chain(FAR) if quote.put_call == PUT]

        readings = fear_readings([*puts, *far], ASOF)

        self.assertIsNone(readings.iv_skew)
        self.assertIsNone(readings.iv_term)

    def test_the_downside_leg_sits_five_percent_below_the_money(self) -> None:
        # 0.90 would read 34.0 and 1.00 would read 20.0 off the same ladder, so a
        # different distance cannot produce this number.
        readings = fear_readings([*_chain(NEAR), *_chain(FAR)], ASOF)

        self.assertEqual(readings.iv_skew, _LADDER[0.95] - _LADDER[1.00])

    def test_upside_volatility_above_downside_is_reported_rather_than_clamped(self) -> None:
        # An inverted smile is an unusual but real state; reporting it as zero would
        # hide the very asymmetry the reading exists to show.
        inverted = dict(_LADDER)
        inverted[0.95] = 15.0

        readings = fear_readings(
            [*_chain(NEAR, ladder=inverted), *_chain(FAR, ladder=inverted)], ASOF
        )

        self.assertEqual(readings.iv_skew, -5.0)

    def test_the_term_reading_is_the_second_month_minus_the_first(self) -> None:
        readings = fear_readings([*_chain(NEAR), *_chain(FAR, level=9.0)], ASOF)

        self.assertEqual(readings.iv_term, 9.0)

    def test_the_constant_maturity_interpolates_in_variance(self) -> None:
        # Interpolating volatility directly would understate the reading whenever
        # the two expiries disagree, which is exactly when the market is scared.
        readings = fear_readings([*_chain(NEAR), *_chain(FAR, level=20.0)], ASOF)

        assert readings.iv_30d is not None
        weight = (44 - 30) / (44 - 16)
        expected = (weight * 20.0**2 + (1 - weight) * 40.0**2) ** 0.5
        self.assertAlmostEqual(readings.iv_30d, expected, places=9)
        self.assertGreater(readings.iv_30d, 30.0)  # above the volatility midpoint

    def test_the_expiry_cutoff_admits_a_week_out_and_nothing_closer(self) -> None:
        # A contract days from settlement prices the SQ auction, not the month. Each
        # expiry around the cutoff carries its own level, so moving the cutoff either
        # way puts a different pair into the term reading.
        six_days, seven_days = date(2026, 8, 4), date(2026, 8, 5)
        self.assertEqual((six_days - ASOF).days, 6)
        self.assertEqual((seven_days - ASOF).days, 7)

        readings = fear_readings(
            [
                *_chain(six_days, level=50.0),
                *_chain(seven_days, level=30.0),
                *_chain(NEAR),
                *_chain(FAR),
            ],
            ASOF,
        )

        # The week-out expiry leads and the 16-day one follows it.
        self.assertEqual(readings.iv_term, -30.0)

    def test_two_expiries_that_both_fall_short_of_the_target_decline_the_level(self) -> None:
        # 16 and 25 days bracket nothing at 30, and extrapolating past the far leg
        # would quote a maturity the chain never priced.
        self.assertLess((MID - ASOF).days, 30)

        readings = fear_readings([*_chain(NEAR), *_chain(MID, level=2.0)], ASOF)

        self.assertIsNone(readings.iv_30d)
        self.assertEqual(readings.iv_term, 2.0)

    def test_a_contract_without_a_quote_is_not_read_as_zero_volatility(self) -> None:
        # The unquoted contract sits exactly where the downside leg looks, and the
        # nearest quoted strike carries a different number, so reading the empty one
        # as zero volatility would be visible in the skew.
        quoted = {rung: vol for rung, vol in _LADDER.items() if rung != 0.95}
        unquoted = [
            OptionQuote(
                put_call=side,
                strike=UNDERLYING * 0.95,
                expiry=expiry,
                implied_volatility=0.0,
                underlying=UNDERLYING,
            )
            for side in (PUT, CALL)
            for expiry in (NEAR, FAR)
        ]

        readings = fear_readings(
            [*unquoted, *_chain(NEAR, ladder=quoted), *_chain(FAR, ladder=quoted)], ASOF
        )

        self.assertEqual(readings.iv_skew, _LADDER[0.975] - _LADDER[1.00])

    def test_an_empty_chain_reads_nothing(self) -> None:
        readings = fear_readings([], ASOF)

        self.assertIsNone(readings.iv_30d)
        self.assertIsNone(readings.iv_skew)
        self.assertIsNone(readings.iv_term)


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

    def test_a_timestamp_expiry_becomes_a_date_the_maturity_can_subtract(self) -> None:
        # A timestamp passes an isinstance check against date but cannot be
        # subtracted from one, which would surface as a crash in the maturity
        # arithmetic rather than as an unreadable row here.
        rows = [self._record(SQD=datetime(2026, 8, 14, 15, 0, tzinfo=ZoneInfo("Asia/Tokyo")))]

        quotes = quotes_from_records(rows, ASOF)

        self.assertEqual([quote.expiry for quote in quotes], [date(2026, 8, 14)])
        self.assertEqual((quotes[0].expiry - ASOF).days, 16)


if __name__ == "__main__":
    unittest.main()
