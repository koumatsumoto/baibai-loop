from __future__ import annotations

import tempfile
import unittest
from collections.abc import Sequence
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from unittest import mock
from zoneinfo import ZoneInfo

from baibai_engine.market_context.indicators.definitions import SeriesDefinition, load_definitions
from baibai_engine.market_context.indicators.providers import jquants_options
from baibai_engine.market_context.indicators.providers.base import (
    FetchContext,
    IndicatorsProviderError,
    ProviderFetchStoppedError,
)
from baibai_engine.market_context.reading.option_iv import (
    CALL,
    PUT,
    SETTLEMENT_SNAPSHOT,
    OptionIvReadings,
    OptionQuote,
    as_date,
    option_iv_readings,
    quotes_from_records,
    usable_expiries,
)

from baibai_engine.market_context.indicator_service import IndicatorsService
from baibai_engine.market_context.indicators.db import initialize_database, insert_observations
from baibai_engine.market_context.indicators.observations import ObservationRecord
from baibai_engine.market_context.indicators.option_chains import read_chains, store_chain, stored_days
from baibai_engine.market_context.indicators.providers.jquants_options import OptionChainSource
from baibai_engine.market_context.option_acquisition import OptionAcquisition
from baibai_engine.market_context.reading.options import OptionSeriesReader

ASOF = date(2026, 7, 29)
NEAR = date(2026, 8, 14)  # 16 days out
MID = date(2026, 8, 23)  # 25 days out
ON_TARGET = date(2026, 8, 28)  # 30 days out — the constant-maturity target itself
FAR = date(2026, 9, 11)  # 44 days out
WIDE = date(2026, 9, 25)  # 58 days out
UNDERLYING = 40_000.0

# Two smiles with different asymmetries: 12.0 points on one and 3.0 on the other, so
# an interpolation between them lands on a number neither leg carries.
_STEEP: dict[float, float] = {0.90: 40.0, 0.95: 32.0, 1.00: 20.0, 1.05: 18.0}
_FLAT: dict[float, float] = {0.90: 30.0, 0.95: 26.0, 1.00: 23.0, 1.05: 22.0}

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


def _registered(series_id: str) -> SeriesDefinition:
    """The series exactly as the shipped registry declares it."""
    return load_definitions().by_id()[series_id]


def _records(
    quotes: Sequence[OptionQuote],
    day: date = ASOF,
    *,
    snapshot: str = SETTLEMENT_SNAPSHOT,
) -> list[dict[str, object]]:
    """Quotes in the shape the provider payload carries them.

    `Date` is the day the chain answers for, and the provider checks it, so every
    fixture carries it. Missing dates are a separate invalid-payload case. `snapshot` builds the second copy an emergency-margin
    session carries, which is the one shape that puts two rows on one contract.
    """
    return [
        {
            "Date": day.isoformat(),
            "SQD": quote.expiry.isoformat(),
            "Strike": quote.strike,
            "IV": quote.implied_volatility,
            "UnderPx": quote.underlying,
            "PCDiv": quote.put_call,
            "EmMrgnTrgDiv": snapshot,
        }
        for quote in quotes
    ]


def _chain_records(day: date = ASOF) -> list[dict[str, object]]:
    return _records([*_chain(NEAR), *_chain(FAR, level=1.0)], day)


def _chain(
    expiry: date,
    *,
    underlying: float = UNDERLYING,
    level: float = 0.0,
    basis_gap: float = 0.0,
    gaps: dict[float, float] | None = None,
    ladder: dict[float, float] | None = None,
    one_sided: bool = False,
) -> list[OptionQuote]:
    """Build synthetic quotes with controlled level and put/call disagreement.

    By default the gap is split symmetrically across the two sides. With
    one_sided=True it moves only the put. These fixtures test the calculation's
    response to each assumption; they do not establish which error structure
    an actual provider has. Per-strike gaps can also change the skew.
    """
    rungs = _LADDER if ladder is None else ladder
    quotes: list[OptionQuote] = []
    for moneyness, volatility in rungs.items():
        strike = underlying * moneyness
        gap = basis_gap if gaps is None else gaps.get(moneyness, 0.0)
        put_share = gap if one_sided else gap / 2
        call_share = 0.0 if one_sided else gap / 2
        for side, vol in (
            (PUT, volatility + level + put_share),
            (CALL, volatility + level - call_share),
        ):
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


class OptionIvReadingsTest(unittest.TestCase):
    def test_equal_distance_strikes_do_not_depend_on_source_row_order(self) -> None:
        ladder = {0.9375: 20.0, 1.0625: 30.0, 1.09375: 40.0}
        quotes = [*_chain(NEAR, ladder=ladder), *_chain(FAR, ladder=ladder)]
        forward = option_iv_readings(quotes, ASOF)
        backward = option_iv_readings(list(reversed(quotes)), ASOF)

        self.assertEqual(forward, backward)
        self.assertEqual(forward.iv_30d, 20.0)
        self.assertEqual(forward.iv_skew, 0.0)
        self.assertEqual(forward.iv_term, 0.0)

    def test_nonpositive_strikes_do_not_enter_the_usable_smile(self) -> None:
        quotes = _chain(NEAR, ladder={-0.1: 20.0, 0.0: 20.0, 1.0: 25.0})
        usable = usable_expiries(quotes, ASOF)

        self.assertEqual({quote.strike for quote in usable[NEAR]}, {UNDERLYING})
        self.assertEqual(len(usable[NEAR]), 2)

    def test_the_atm_average_is_where_it_was_when_the_sides_disagree(self) -> None:
        # Equal opposite offsets cancel in this synthetic fixture. This does not
        # establish the error structure of actual quotes or immunity to source errors.
        clean = option_iv_readings([*_chain(NEAR), *_chain(FAR)], ASOF)

        for gap in (4.0, 11.0):
            with self.subTest(gap=gap):
                readings = option_iv_readings(
                    [*_chain(NEAR, basis_gap=gap), *_chain(FAR, basis_gap=gap)], ASOF
                )

                assert readings.iv_30d is not None
                assert clean.iv_30d is not None
                self.assertAlmostEqual(readings.iv_30d, clean.iv_30d, places=9)
                self.assertAlmostEqual(readings.iv_30d, _LADDER[1.00], places=9)

    def test_a_disagreement_carried_by_one_side_would_move_the_average(self) -> None:
        # Contrast symmetric and one-sided synthetic errors. Passing the symmetric
        # case alone would not establish robustness to the one-sided case.
        clean = option_iv_readings([*_chain(NEAR), *_chain(FAR)], ASOF)
        split = option_iv_readings(
            [*_chain(NEAR, basis_gap=8.0), *_chain(FAR, basis_gap=8.0)], ASOF
        )
        lopsided = option_iv_readings(
            [
                *_chain(NEAR, basis_gap=8.0, one_sided=True),
                *_chain(FAR, basis_gap=8.0, one_sided=True),
            ],
            ASOF,
        )

        assert clean.iv_30d is not None
        assert split.iv_30d is not None
        assert lopsided.iv_30d is not None
        self.assertAlmostEqual(split.iv_30d, clean.iv_30d, places=9)
        self.assertAlmostEqual(lopsided.iv_30d - clean.iv_30d, 4.0, places=9)

    def test_the_at_the_money_average_reaches_for_a_strike_quoting_both_sides(self) -> None:
        # The level requires both sides at one strike. If the nearest strike lacks
        # a call, select the nearest paired strike rather than a put-only value.
        unpaired = {UNDERLYING, UNDERLYING * 1.025}
        half_quoted = [
            quote
            for quote in _chain(NEAR)
            if not (quote.strike in unpaired and quote.put_call == CALL)
        ]

        readings = option_iv_readings([*half_quoted, *_chain(FAR)], ASOF)
        put_only = option_iv_readings(
            [quote for quote in (*_chain(NEAR), *_chain(FAR)) if quote.put_call == PUT], ASOF
        )

        # 0.975 is now the nearest rung quoting both, so the near leg reads 25.0 rather
        # than the 20.0 a put-only read of the money would have given.
        weight = (44 - 30) / (44 - 16)
        expected = (weight * _LADDER[0.975] ** 2 + (1 - weight) * _LADDER[1.00] ** 2) ** 0.5
        assert readings.iv_30d is not None
        self.assertAlmostEqual(readings.iv_30d, expected, places=9)
        self.assertEqual(put_only, OptionIvReadings())

    def test_the_skew_is_the_downside_put_minus_the_at_the_money_put(self) -> None:
        readings = option_iv_readings([*_chain(NEAR), *_chain(FAR)], ASOF)

        self.assertEqual(readings.iv_skew, 10.0)

    def test_a_uniform_disagreement_moves_both_skew_legs_together(self) -> None:
        # Both legs come from the put side, so a disagreement of the same size at every
        # strike lifts them equally and leaves the difference alone. A leg taken off
        # the call side would instead carry the whole of it.
        clean = option_iv_readings([*_chain(NEAR), *_chain(FAR)], ASOF)
        skewed = option_iv_readings(
            [*_chain(NEAR, basis_gap=10.0), *_chain(FAR, basis_gap=10.0)], ASOF
        )

        self.assertEqual(skewed.iv_skew, clean.iv_skew)

    def test_a_disagreement_that_differs_across_strikes_does_move_the_skew(self) -> None:
        # A strike-specific offset changes the skew even when the median basis
        # check passes. The bound is not a guarantee that the remaining error is small.
        uneven = {0.95: 8.0}
        clean = option_iv_readings([*_chain(NEAR), *_chain(FAR)], ASOF)
        skewed = option_iv_readings([*_chain(NEAR, gaps=uneven), *_chain(FAR, gaps=uneven)], ASOF)

        assert clean.iv_skew is not None
        assert skewed.iv_skew is not None
        self.assertAlmostEqual(skewed.iv_skew - clean.iv_skew, 4.0, places=9)

    def test_sides_that_disagree_beyond_the_bound_withhold_the_differences(self) -> None:
        # Above the operational bound the differences are withheld. The level is
        # retained; only this symmetric fixture guarantees cancellation.
        ladder = {0.90: 50.0, 0.95: 46.0, 0.975: 42.0, 1.00: 40.0, 1.025: 38.0, 1.05: 36.0}

        readings = option_iv_readings(
            [
                *_chain(NEAR, ladder=ladder, basis_gap=25.0),
                *_chain(FAR, ladder=ladder, basis_gap=25.0),
            ],
            ASOF,
        )

        self.assertIsNone(readings.iv_skew)
        self.assertIsNone(readings.iv_term)
        self.assertIsNotNone(readings.iv_30d)

    def test_a_gap_inside_the_bound_leaves_the_readings_alone(self) -> None:
        # This fixture falls inside the chosen tolerance and keeps the difference
        # readings available. Availability does not prove the numbers are unchanged.
        ladder = {0.90: 50.0, 0.95: 46.0, 0.975: 42.0, 1.00: 40.0, 1.025: 38.0, 1.05: 36.0}

        readings = option_iv_readings(
            [
                *_chain(NEAR, ladder=ladder, basis_gap=11.0),
                *_chain(FAR, ladder=ladder, basis_gap=11.0),
            ],
            ASOF,
        )

        self.assertIsNotNone(readings.iv_skew)
        self.assertIsNotNone(readings.iv_term)

    def test_the_basis_check_reads_the_strikes_around_the_money_not_only_the_money(self) -> None:
        # A source can agree at one strike and disagree either side of it, so a check
        # that only looked at the money would pass a chain it should refuse.
        ladder = {0.90: 50.0, 0.95: 46.0, 0.975: 42.0, 1.00: 40.0, 1.025: 38.0, 1.05: 36.0}
        around = {0.95: 25.0, 0.975: 25.0, 1.025: 25.0, 1.05: 25.0}

        readings = option_iv_readings(
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

        readings = option_iv_readings(
            [
                *_chain(NEAR, ladder=ladder, gaps=wings),
                *_chain(FAR, ladder=ladder, gaps=wings),
            ],
            ASOF,
        )

        self.assertEqual(readings.iv_skew, 10.0)

    def test_calls_quoted_above_puts_are_refused_the_same_as_below(self) -> None:
        # The basis error runs both ways in the source, so the check has to read the
        # size of the disagreement rather than its direction.
        ladder = {0.90: 50.0, 0.95: 46.0, 0.975: 42.0, 1.00: 40.0, 1.025: 38.0, 1.05: 36.0}
        inverted = {0.95: -25.0, 0.975: -25.0, 1.025: -25.0, 1.05: -25.0}

        readings = option_iv_readings(
            [
                *_chain(NEAR, ladder=ladder, gaps=inverted),
                *_chain(FAR, ladder=ladder, gaps=inverted),
            ],
            ASOF,
        )

        self.assertIsNone(readings.iv_skew)

    def test_low_iv_filter_is_calculation_policy_not_missing_source_data(self) -> None:
        records = [
            {
                "EmMrgnTrgDiv": "002",
                "PCDiv": PUT,
                "SQD": NEAR.isoformat(),
                "Strike": UNDERLYING,
                "IV": value,
                "UnderPx": UNDERLYING,
            }
            for value in (0.5, 1.0, 1.01)
        ]
        quotes = quotes_from_records(records)
        self.assertEqual([quote.implied_volatility for quote in quotes], [0.5, 1.0, 1.01])
        selected = usable_expiries(quotes, ASOF)
        self.assertEqual([quote.implied_volatility for quote in selected[NEAR]], [1.01])

    def test_a_chain_quoting_only_puts_withholds_the_differences(self) -> None:
        # With nothing to compare the sides against, the basis is unknown rather
        # than sound.
        puts = [quote for quote in _chain(NEAR) if quote.put_call == PUT]
        far = [quote for quote in _chain(FAR) if quote.put_call == PUT]

        readings = option_iv_readings([*puts, *far], ASOF)

        self.assertIsNone(readings.iv_skew)
        self.assertIsNone(readings.iv_term)

    def test_the_downside_leg_sits_five_percent_below_the_money(self) -> None:
        # 0.90 would read 34.0 and 1.00 would read 20.0 off the same ladder, so a
        # different distance cannot produce this number.
        readings = option_iv_readings([*_chain(NEAR), *_chain(FAR)], ASOF)

        self.assertEqual(readings.iv_skew, _LADDER[0.95] - _LADDER[1.00])

    def test_upside_volatility_above_downside_is_reported_rather_than_clamped(self) -> None:
        # An inverted smile is an unusual but real state; reporting it as zero would
        # hide the very asymmetry the reading exists to show.
        inverted = dict(_LADDER)
        inverted[0.95] = 15.0

        readings = option_iv_readings(
            [*_chain(NEAR, ladder=inverted), *_chain(FAR, ladder=inverted)], ASOF
        )

        self.assertEqual(readings.iv_skew, -5.0)

    def test_the_term_reading_is_the_second_month_minus_the_first(self) -> None:
        readings = option_iv_readings([*_chain(NEAR), *_chain(FAR, level=9.0)], ASOF)

        self.assertEqual(readings.iv_term, 9.0)

    def test_the_constant_maturity_interpolates_in_variance(self) -> None:
        # The chosen squared-level interpolation differs from linear volatility;
        # this test verifies the convention, not a market state or forecasting claim.
        # The two expiries straddle the target unevenly, so a weight that reads the
        # wrong side of the bracket lands on a different number.
        self.assertNotEqual((NEAR - ASOF).days + (WIDE - ASOF).days, 2 * 30)

        readings = option_iv_readings([*_chain(NEAR), *_chain(WIDE, level=20.0)], ASOF)

        assert readings.iv_30d is not None
        weight = (58 - 30) / (58 - 16)
        expected = (weight * 20.0**2 + (1 - weight) * 40.0**2) ** 0.5
        self.assertAlmostEqual(readings.iv_30d, expected, places=9)
        # Interpolating the volatilities directly would put it here instead.
        self.assertGreater(readings.iv_30d, weight * 20.0 + (1 - weight) * 40.0)

    def test_the_term_reading_uses_the_next_expiry_not_the_last_one(self) -> None:
        # A chain carries two dozen expiries, so reaching past the second one would
        # quote a contract months away as if it were next month.
        readings = option_iv_readings(
            [*_chain(NEAR), *_chain(FAR, level=9.0), *_chain(WIDE, level=25.0)], ASOF
        )

        self.assertEqual(readings.iv_term, 9.0)

    def test_the_expiry_cutoff_admits_a_week_out_and_nothing_closer(self) -> None:
        # Fix the chosen seven-day cutoff independently of the implementation.
        # Different levels make an off-by-one error change the selected pair.
        six_days, seven_days = date(2026, 8, 4), date(2026, 8, 5)
        self.assertEqual((six_days - ASOF).days, 6)
        self.assertEqual((seven_days - ASOF).days, 7)

        readings = option_iv_readings(
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

        readings = option_iv_readings([*_chain(NEAR), *_chain(MID, level=2.0)], ASOF)

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

        readings = option_iv_readings(
            [*unquoted, *_chain(NEAR, ladder=quoted), *_chain(FAR, ladder=quoted)], ASOF
        )

        self.assertEqual(readings.iv_skew, _LADDER[0.975] - _LADDER[1.00])

    def test_the_skew_is_interpolated_to_the_target_maturity(self) -> None:
        # A skew read off whichever expiry happens to be in front is a different
        # quantity every week of the settlement cycle, and a reader comparing it to a
        # pooled quantile would find fear on the calendar. The two expiries here
        # carry different asymmetries, so the weight is visible in the answer:
        # bracketing from the wrong side, or in the wrong direction, lands elsewhere.
        self.assertEqual(((NEAR - ASOF).days, (WIDE - ASOF).days), (16, 58))

        readings = option_iv_readings(
            [*_chain(NEAR, ladder=_STEEP), *_chain(WIDE, ladder=_FLAT)], ASOF
        )

        weight = (30**-0.5 - 58**-0.5) / (16**-0.5 - 58**-0.5)
        assert readings.iv_skew is not None
        self.assertAlmostEqual(readings.iv_skew, weight * 12.0 + (1 - weight) * 3.0, places=9)
        self.assertNotEqual(readings.iv_skew, 12.0)

    def test_an_expiry_landing_on_the_target_is_inside_the_bracket(self) -> None:
        # 30 days is the target itself, so an expiry sitting exactly on it brackets
        # the target rather than missing it — and answers without interpolating at
        # all, which is the one case where the reading is the contract's own number.
        self.assertEqual((ON_TARGET - ASOF).days, 30)

        leading = option_iv_readings(
            [*_chain(ON_TARGET, ladder=_STEEP), *_chain(FAR, ladder=_FLAT)], ASOF
        )
        trailing = option_iv_readings(
            [*_chain(NEAR, ladder=_STEEP), *_chain(ON_TARGET, ladder=_FLAT)], ASOF
        )

        self.assertEqual((leading.iv_skew, leading.iv_30d), (12.0, 20.0))
        self.assertEqual((trailing.iv_skew, trailing.iv_30d), (3.0, 23.0))

    def test_a_chain_with_one_priced_expiry_answers_nothing(self) -> None:
        # Every reading needs two expiries: the differences by definition, and the
        # level because one expiry past the target would have to be reported as a
        # 30-day contract the chain never priced. Reaching for a second expiry that is
        # not there must decline rather than fail the whole day.
        far_out = option_iv_readings(list(_chain(WIDE)), ASOF)
        on_target = option_iv_readings(list(_chain(ON_TARGET)), ASOF)
        too_near = option_iv_readings(list(_chain(NEAR)), ASOF)

        self.assertEqual(far_out, OptionIvReadings())
        self.assertEqual(on_target, OptionIvReadings())
        self.assertEqual(too_near, OptionIvReadings())

    def test_the_basis_check_reaches_the_second_expiry_the_differences_read(self) -> None:
        # Both differences are built from the second expiry as well as the first, so a
        # check that only saw the front would pass a chain whose second month is the
        # broken one — and a term reading built on it can invert its own sign.
        ladder = {0.90: 50.0, 0.95: 46.0, 0.975: 42.0, 1.00: 40.0, 1.025: 38.0, 1.05: 36.0}

        clean = option_iv_readings(
            [*_chain(NEAR, ladder=ladder), *_chain(FAR, ladder=ladder, level=4.0)], ASOF
        )
        far_broken = option_iv_readings(
            [
                *_chain(NEAR, ladder=ladder),
                *_chain(FAR, ladder=ladder, level=4.0, basis_gap=30.0),
            ],
            ASOF,
        )

        self.assertEqual(clean.iv_term, 4.0)
        self.assertIsNone(far_broken.iv_term)
        self.assertIsNone(far_broken.iv_skew)

    def test_one_row_without_a_believable_underlying_does_not_move_the_readings(self) -> None:
        # The source repeats the underlying on every row, so any single row could be
        # taken for it — and a slipped decimal would relocate the at-the-money strike,
        # the basis band and both skew legs while every reading stayed plausible.
        slipped = [
            OptionQuote(
                put_call=side,
                strike=UNDERLYING,
                expiry=expiry,
                implied_volatility=20.0,
                underlying=UNDERLYING / 100,
            )
            for side in (PUT, CALL)
            for expiry in (NEAR, FAR)
        ]
        clean = option_iv_readings([*_chain(NEAR), *_chain(FAR)], ASOF)

        readings = option_iv_readings([*slipped, *_chain(NEAR), *_chain(FAR)], ASOF)

        self.assertEqual(readings, clean)

    def test_the_basis_bound_admits_the_gap_it_names_and_refuses_the_next_one(self) -> None:
        # Fix the inclusive operational threshold, not a guarantee about future
        # source behaviour or the accuracy of an admitted observation.
        ladder = {0.90: 50.0, 0.95: 46.0, 0.975: 42.0, 1.00: 40.0, 1.025: 38.0, 1.05: 36.0}

        def readings_at(gap: float) -> OptionIvReadings:
            return option_iv_readings(
                [
                    *_chain(NEAR, ladder=ladder, basis_gap=gap),
                    *_chain(FAR, ladder=ladder, basis_gap=gap),
                ],
                ASOF,
            )

        self.assertIsNotNone(readings_at(12.0).iv_skew)
        self.assertIsNone(readings_at(12.5).iv_skew)

    def test_the_basis_check_reads_the_edges_of_its_band(self) -> None:
        # The band is a fraction of the underlying, so where it sits and whether its
        # edges are inside both decide which strikes vote. Here the two edge strikes
        # carry the whole disagreement and the strikes outside are clean, so a band
        # that excluded its edges or reached past them would take the other verdict.
        ladder = {0.80: 70.0, 0.90: 80.0, 0.95: 70.0, 1.00: 65.0, 1.10: 75.0, 1.20: 70.0}
        edges = {0.90: 60.0, 1.10: 60.0}

        readings = option_iv_readings(
            [*_chain(NEAR, ladder=ladder, gaps=edges), *_chain(FAR, ladder=ladder, gaps=edges)],
            ASOF,
        )

        self.assertIsNone(readings.iv_skew)

    def test_the_basis_verdict_is_the_middle_gap_not_an_extreme_one(self) -> None:
        # One strike the source got wrong is routine and must not empty the day; a
        # disagreement that is the chain's normal state must. A verdict taken from
        # the wrong element of the sorted gaps reverses both.
        four = {0.90: 50.0, 0.95: 46.0, 1.00: 40.0, 1.05: 36.0}
        five = {0.90: 50.0, 0.95: 46.0, 0.975: 43.0, 1.00: 40.0, 1.05: 36.0}

        def readings_for(ladder: dict[float, float], gaps: dict[float, float]) -> OptionIvReadings:
            return option_iv_readings(
                [*_chain(NEAR, ladder=ladder, gaps=gaps), *_chain(FAR, ladder=ladder, gaps=gaps)],
                ASOF,
            )

        minority = readings_for(four, {1.00: 16.0, 1.05: 16.0})
        majority = readings_for(five, {0.975: 16.0, 1.00: 16.0, 1.05: 16.0})

        self.assertIsNotNone(minority.iv_skew)
        self.assertIsNone(majority.iv_skew)

    def test_a_quote_with_no_underlying_price_is_not_read(self) -> None:
        # The underlying is repeated on every row, and one row carrying zero would
        # put the basis band at zero, empty the check and read a sound chain as
        # unusable.
        broken = [
            OptionQuote(
                put_call=side,
                strike=UNDERLYING,
                expiry=expiry,
                implied_volatility=20.0,
                underlying=0.0,
            )
            for side in (PUT, CALL)
            for expiry in (NEAR, FAR)
        ]
        clean = option_iv_readings([*_chain(NEAR), *_chain(FAR)], ASOF)

        readings = option_iv_readings([*broken, *_chain(NEAR), *_chain(FAR)], ASOF)

        self.assertEqual(readings, clean)

    def test_an_empty_chain_reads_nothing(self) -> None:
        readings = option_iv_readings([], ASOF)

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
            "EmMrgnTrgDiv": SETTLEMENT_SNAPSHOT,
        }
        record.update(overrides)
        return record

    def test_only_the_settlement_snapshot_is_parsed(self) -> None:
        # An emergency-margin session answers with two full copies of the chain,
        # quoted against different prices. Reading both would put a basis error into
        # every strike that carries a row in each.
        rows = [self._record(), self._record(EmMrgnTrgDiv="001", IV=41.6, UnderPx=39000.0)]

        quotes = quotes_from_records(rows)

        self.assertEqual([quote.implied_volatility for quote in quotes], [20.0])
        self.assertEqual([quote.underlying for quote in quotes], [40000.0])

    def test_rows_the_reader_cannot_believe_are_dropped(self) -> None:
        # Synthetic missing, unsupported and non-finite values must not become
        # numeric quotes. This fixture is not evidence of live source frequencies.
        cases: tuple[tuple[str, list[dict[str, object]]], ...] = (
            (
                # Keeping unmarked rows would restore the snapshot mixing as soon as
                # the source stopped sending the field, which is exactly when nothing
                # would notice.
                "no_snapshot_marker",
                [self._record(EmMrgnTrgDiv=None), self._record(EmMrgnTrgDiv="")],
            ),
            ("unknown_side", [self._record(PCDiv="9")]),
            (
                # Non-finite inputs must not pass numeric comparison checks or
                # become usable quotes. This is not a claim about native JSON values.
                "not_a_number",
                [self._record(IV=float("nan")), self._record(UnderPx=float("inf"))],
            ),
        )

        for name, rows in cases:
            with self.subTest(case=name):
                self.assertEqual(quotes_from_records(rows), [])

    def test_a_row_missing_a_field_is_dropped_rather_than_failing(self) -> None:
        # A chain always carries contracts with no quote; a day is still readable.
        rows = [self._record(), self._record(IV=None), self._record(Strike="")]

        self.assertEqual(len(quotes_from_records(rows)), 1)

    def test_native_expiry_is_read_without_inventing_a_timestamp(self) -> None:
        quotes = quotes_from_records([self._record(SQD="2026-08-14")])
        self.assertEqual([quote.expiry for quote in quotes], [date(2026, 8, 14)])

    def test_native_date_parser_does_not_accept_timestamp_objects(self) -> None:
        rows = [self._record(SQD=datetime(2026, 8, 14, 15, 0, tzinfo=ZoneInfo("Asia/Tokyo")))]
        self.assertEqual(quotes_from_records(rows), [])
        self.assertIsNone(as_date("2026-08-14T15:00:00"))


class OptionSourceContractTest(unittest.TestCase):
    def test_the_registry_bands_hold_every_reading_and_catch_an_inflated_one(self) -> None:
        # Retained historical sample extrema fit the configured bands, while the
        # illustrated 100x scale errors do not. These checks do not guarantee that
        # future observations or every scaling error are classified correctly.
        for series_id, sampled_low, sampled_high in (
            ("jp.n225_iv_30d", 11.51, 65.95),
            ("jp.n225_iv_skew", 0.24, 6.73),
            ("jp.n225_iv_term", -14.84, 4.03),
        ):
            series = _registered(series_id)
            low, high = series.plausible_min, series.plausible_max
            assert low is not None
            assert high is not None
            with self.subTest(series=series_id):
                self.assertLess(low, sampled_low)
                self.assertGreater(high, sampled_high)
                self.assertGreater(sampled_high * 100, high)  # a percent read as a ratio

        level = _registered("jp.n225_iv_30d")
        assert level.plausible_min is not None
        self.assertGreater(level.plausible_min, 0.19)  # and a ratio read as a percent

    def test_a_missing_key_is_named_rather_than_failing_at_the_endpoint(self) -> None:
        # Without this the run reaches the API and comes back with an auth error that
        # says nothing about which credential the operator forgot.
        with (
            mock.patch.object(jquants_options, "load_project_env"),
            mock.patch.dict(jquants_options.os.environ, {"JQUANTS_API_KEY": ""}, clear=False),
            self.assertRaises(IndicatorsProviderError) as caught,
        ):
            jquants_options._read_api_key()

        self.assertIn("JQUANTS_API_KEY", str(caught.exception))

    def test_missing_or_invalid_chain_dates_are_not_inferred_from_request(self) -> None:
        # Native JSON dates remain strings; an absent or malformed date cannot
        # establish the requested session even when another row names it.
        rows = _chain_records()

        jquants_options._require_requested_day(rows, ASOF)

        with self.assertRaises(IndicatorsProviderError):
            jquants_options._require_requested_day(rows, ASOF + timedelta(1))

        for invalid in (None, "not-a-date", "2026-07-29T00:00:00", ASOF):
            with self.subTest(invalid=invalid):
                partial = _chain_records()
                partial[0]["Date"] = invalid
                for candidate in (partial, partial[:1]):
                    with self.assertRaisesRegex(IndicatorsProviderError, "observation date"):
                        jquants_options._require_requested_day(candidate, ASOF)
        jquants_options._require_requested_day([], ASOF)

class OptionAcquisitionTest(unittest.TestCase):
    """Native source retention and L2 readings through the application boundary."""

    def _database(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        connection = initialize_database(Path(temporary.name) / "indicators.sqlite")
        self.addCleanup(connection.close)
        return connection

    def _context(self, purpose="refresh"):
        context = FetchContext(purpose=purpose)
        self.addCleanup(context.close)
        return context

    def test_three_readings_acquire_each_weekday_once_and_keep_native_fields(self) -> None:
        connection = self._database()
        acquired = []

        def source(day):
            acquired.append(day)
            rows = _chain_records(day)
            rows[0]["UnselectedNativeField"] = None
            rows[0]["NativeIdentifier"] = "0000123"
            return rows

        with mock.patch.object(OptionChainSource, "fetch", side_effect=source), mock.patch.object(
            OptionChainSource, "__init__", return_value=None
        ):
            acquisition = OptionAcquisition(connection, self._context())
            for field in ("n225_iv_30d", "n225_iv_skew", "n225_iv_term"):
                acquisition.query(
                    _registered(f"jp.{field}"), start=date(2026, 7, 27), end=ASOF, refresh=True
                )
        self.assertEqual(acquired, [date(2026, 7, 27), date(2026, 7, 28), ASOF])
        chains = list(read_chains(connection, acquired[0], ASOF))
        self.assertEqual(len(chains), 3)
        self.assertIsNone(chains[0].records[0]["UnselectedNativeField"])
        self.assertEqual(chains[0].records[0]["NativeIdentifier"], "0000123")
        self.assertEqual(connection.execute("SELECT COUNT(*) FROM observations").fetchone()[0], 0)

    def test_weekends_are_skipped_and_range_end_is_included(self) -> None:
        connection = self._database()
        with mock.patch.object(OptionChainSource, "__init__", return_value=None), mock.patch.object(
            OptionChainSource, "fetch", side_effect=lambda day: _chain_records(day)
        ) as fetch:
            OptionAcquisition(connection, self._context()).query(
                _registered("jp.n225_iv_term"),
                start=date(2026, 7, 31), end=date(2026, 8, 3), refresh=True,
            )
        self.assertEqual(fetch.call_args_list, [mock.call(date(2026, 7, 31)), mock.call(date(2026, 8, 3))])

    def test_completed_prefix_survives_failure_and_next_pass_resumes(self) -> None:
        connection = self._database()
        first, second = date(2026, 7, 27), date(2026, 7, 28)
        with mock.patch.object(OptionChainSource, "__init__", return_value=None), mock.patch.object(
            OptionChainSource, "fetch", side_effect=[_chain_records(first), ProviderFetchStoppedError("stopped")]
        ) as fetch:
            acquisition = OptionAcquisition(connection, self._context())
            for field in ("n225_iv_30d", "n225_iv_term"):
                with self.assertRaisesRegex(ProviderFetchStoppedError, "stopped"):
                    acquisition.query(_registered(f"jp.{field}"), start=first, end=second, refresh=True)
            self.assertEqual(fetch.call_count, 2)
        self.assertEqual(stored_days(connection, first, second), {first})
        with mock.patch.object(OptionChainSource, "__init__", return_value=None), mock.patch.object(
            OptionChainSource, "fetch", return_value=_chain_records(second)
        ) as fetch:
            OptionAcquisition(connection, self._context("read")).query(
                _registered("jp.n225_iv_30d"), start=first, end=second, refresh=False
            )
        fetch.assert_called_once_with(second)
        self.assertEqual(stored_days(connection, first, second), {first, second})

    def test_calculation_failure_keeps_original_and_does_not_refetch_it(self) -> None:
        connection = self._database()
        rows = _chain_records()
        rows.append(dict(rows[0], Strike=str(rows[0]["Strike"])))
        with mock.patch.object(OptionChainSource, "__init__", return_value=None), mock.patch.object(
            OptionChainSource, "fetch", return_value=rows
        ) as fetch:
            with self.assertRaisesRegex(ValueError, "duplicate settlement"):
                OptionAcquisition(connection, self._context()).query(
                    _registered("jp.n225_iv_30d"), start=ASOF, end=ASOF, refresh=True
                )
            with self.assertRaisesRegex(ValueError, "duplicate settlement"):
                OptionAcquisition(connection, self._context("read")).query(
                    _registered("jp.n225_iv_30d"), start=ASOF, end=ASOF, refresh=False
                )
        fetch.assert_called_once_with(ASOF)
        self.assertEqual(list(read_chains(connection, ASOF, ASOF))[0].records, tuple(rows))

    def test_empty_replacement_hides_previous_chain_and_old_scalar_values(self) -> None:
        connection = self._database()
        before = datetime(2026, 7, 30, tzinfo=UTC)
        with connection:
            store_chain(connection, trade_date=ASOF, fetched_at=before, records=_chain_records())
            insert_observations(connection, [ObservationRecord(
                series_id="jp.n225_iv_30d", observed_at=ASOF, value=25.0, unit="percent",
                source_url=_registered("jp.n225_iv_30d").source_url, vintage_at=before,
            )])
        self.assertTrue(OptionSeriesReader(connection).read(_registered("jp.n225_iv_30d"), start=ASOF, end=ASOF))
        with connection:
            store_chain(connection, trade_date=ASOF, fetched_at=before + timedelta(days=1), records=[])
        self.assertEqual(OptionSeriesReader(connection).read(_registered("jp.n225_iv_30d"), start=ASOF, end=ASOF), ())
        self.assertEqual(stored_days(connection, ASOF, ASOF), {ASOF})
        self.assertEqual(connection.execute("SELECT COUNT(*) FROM observations").fetchone()[0], 1)
        self.assertEqual(connection.execute("SELECT COUNT(*) FROM jquants_option_chains").fetchone()[0], 2)

    def test_identical_response_does_not_add_another_payload(self) -> None:
        connection = self._database()
        fetched_at = datetime(2026, 7, 30, tzinfo=UTC)
        with connection:
            self.assertTrue(store_chain(connection, trade_date=ASOF, fetched_at=fetched_at, records=_chain_records()))
            self.assertFalse(store_chain(connection, trade_date=ASOF, fetched_at=fetched_at + timedelta(days=1), records=_chain_records()))
        self.assertEqual(connection.execute("SELECT COUNT(*) FROM jquants_option_chains").fetchone()[0], 1)

    def test_emergency_snapshot_is_retained_but_only_settlement_is_calculated(self) -> None:
        connection = self._database()
        settled = _chain_records()
        intervention = _records([*_chain(NEAR, underlying=39_000.0), *_chain(FAR, underlying=39_000.0)], snapshot="001")
        with connection:
            store_chain(connection, trade_date=ASOF, fetched_at=datetime.now(UTC), records=[*settled, *intervention])
        result = OptionSeriesReader(connection).read(_registered("jp.n225_iv_30d"), start=ASOF, end=ASOF)
        self.assertEqual(result[0].value, option_iv_readings(quotes_from_records(settled), ASOF).iv_30d)
        self.assertEqual(len(list(read_chains(connection, ASOF, ASOF))[0].records), len(settled) + len(intervention))

    def test_missing_maturity_bracket_is_not_an_empty_source_response(self) -> None:
        connection = self._database()
        day = ASOF - timedelta(days=1)
        rows = _records([*_chain(ON_TARGET), *_chain(WIDE, level=2.0)], day)
        with connection:
            store_chain(connection, trade_date=day, fetched_at=datetime.now(UTC), records=rows)
        self.assertEqual(OptionSeriesReader(connection).read(_registered("jp.n225_iv_30d"), start=day, end=day), ())
        self.assertTrue(OptionSeriesReader(connection).read(_registered("jp.n225_iv_term"), start=day, end=day))
        self.assertEqual(list(read_chains(connection, day, day))[0].records, tuple(rows))

    def test_latest_rechecks_an_empty_response_before_publication(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        database = Path(temporary.name) / "indicators.sqlite"
        connection = initialize_database(database)
        try:
            with connection:
                store_chain(connection, trade_date=ASOF, fetched_at=datetime(2026, 7, 29, tzinfo=UTC), records=[])
        finally:
            connection.close()
        with mock.patch("baibai_engine.market_context.indicator_service._today_jst", return_value=ASOF), mock.patch.object(
            OptionChainSource, "__init__", return_value=None
        ), mock.patch.object(OptionChainSource, "close"), mock.patch.object(
            OptionChainSource, "fetch", side_effect=lambda day: _chain_records(day) if day == ASOF else []
        ) as fetch:
            result = IndicatorsService(database).get_latest("jp.n225_iv_30d")
        self.assertEqual([row.observed_at for row in result.observations], [ASOF])
        self.assertIn(mock.call(ASOF), fetch.call_args_list)

    def test_rebuild_reuses_old_complete_days_but_bounded_refresh_can_restate_them(self) -> None:
        connection = self._database()
        start = ASOF - timedelta(days=30)
        days = [start + timedelta(days=i) for i in range(31)
                if (start + timedelta(days=i)).weekday() < 5]
        with connection:
            for day in days:
                store_chain(connection, trade_date=day, fetched_at=datetime(2026, 7, 30, tzinfo=UTC), records=[])
        with mock.patch.object(OptionChainSource, "__init__", return_value=None), mock.patch.object(
            OptionChainSource, "fetch", return_value=[]
        ) as fetch:
            OptionAcquisition(connection, self._context("rebuild")).query(
                _registered("jp.n225_iv_term"), start=start, end=ASOF, refresh=True
            )
            self.assertEqual(fetch.call_args_list, [mock.call(day) for day in days if day >= ASOF - timedelta(days=14)])
            fetch.reset_mock()
            OptionAcquisition(connection, self._context("refresh")).query(
                _registered("jp.n225_iv_term"), start=days[0], end=days[0], refresh=True
            )
            fetch.assert_called_once_with(days[0])
