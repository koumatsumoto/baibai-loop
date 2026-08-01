from __future__ import annotations

import unittest
from collections.abc import Sequence
from datetime import date, datetime, timedelta
from types import SimpleNamespace
from typing import cast
from unittest import mock
from zoneinfo import ZoneInfo

from requests.exceptions import ConnectionError as RequestsConnectionError
from requests.exceptions import RetryError, Timeout
from tools.research.option_iv_sample import business_days, quantiles
from tools.research.option_iv_sample import main as sample_main

from baibai_engine.macro.indicators.definitions import SeriesDefinition, load_definitions
from baibai_engine.macro.indicators.providers import jquants_options
from baibai_engine.macro.indicators.providers.base import HttpSession, IndicatorsProviderError
from baibai_engine.macro.indicators.providers.option_iv import (
    CALL,
    PUT,
    FearReadings,
    OptionQuote,
    as_date,
    fear_readings,
    quotes_from_records,
)

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


def RuntimeError_with(response: object, message: str = "boom") -> Exception:
    """An exception shaped the way jquantsapi raises one: the response is attached."""
    error = RuntimeError(message)
    error.response = response  # type: ignore[attr-defined]  # mirrors requests.HTTPError
    return error


class _FakeFrame:
    """Only the two members the provider reads off a DataFrame."""

    def __init__(self, records: list[dict[str, object]]) -> None:
        self._records = records

    def to_dict(self, orient: str) -> list[dict[str, object]]:
        assert orient == "records"
        return list(self._records)


def _records(quotes: Sequence[OptionQuote], day: date = ASOF) -> list[dict[str, object]]:
    """Quotes in the shape the provider payload carries them.

    `Date` is the day the chain answers for, and the provider checks it, so every
    fixture carries it — a payload without it would exercise only the branch that
    has nothing to compare.
    """
    return [
        {
            "Date": day,
            "SQD": quote.expiry.isoformat(),
            "Strike": quote.strike,
            "IV": quote.implied_volatility,
            "UnderPx": quote.underlying,
            "PCDiv": quote.put_call,
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
    """One expiry's quotes.

    `level` shifts the whole smile, so a test can tell two expiries apart. The gap
    reproduces a chain whose two sides were not put on one footing, and it is split
    between them rather than applied to one: the source's disagreement is two-sided,
    the sampled session with a 20-point gap reading 46.5 on the put against 28.0 on
    the call while their average stayed with its neighbours. A one-sided gap would
    move that average by half the gap and make every reading built on it look fragile
    for a reason the source does not produce. `gaps` sets it per moneyness so a test
    can put the disagreement where it wants it; `basis_gap` applies one everywhere.

    `one_sided` puts the whole gap on the put instead of splitting it. That is not the
    shape the source produces, and a suite that could only build the split form would
    be checking its own fixture rather than the readings — so both are available and
    the difference between them is asserted.
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


class FearReadingsTest(unittest.TestCase):
    def test_the_atm_average_is_where_it_was_when_the_sides_disagree(self) -> None:
        # Each side carries its own directional bias, so the average is closer to what
        # the market charges for movement rather than for direction — and it is also
        # what survives the source's own inconsistency, since at the money a wrong
        # reference price pushes the two sides apart by a like amount.
        clean = fear_readings([*_chain(NEAR), *_chain(FAR)], ASOF)

        for gap in (4.0, 11.0):
            with self.subTest(gap=gap):
                readings = fear_readings(
                    [*_chain(NEAR, basis_gap=gap), *_chain(FAR, basis_gap=gap)], ASOF
                )

                assert readings.iv_30d is not None
                assert clean.iv_30d is not None
                self.assertAlmostEqual(readings.iv_30d, clean.iv_30d, places=9)
                self.assertAlmostEqual(readings.iv_30d, _LADDER[1.00], places=9)

    def test_a_disagreement_carried_by_one_side_would_move_the_average(self) -> None:
        # The reading survives the source's disagreement because that disagreement is
        # two-sided — measured over 437 sessions, a departure regressed on the signed
        # gap has slope +0.04 where one-sidedness predicts 0.5. A suite that could only
        # build the two-sided form would prove nothing about which shape it survives,
        # so the other shape is built here and the difference is what is asserted.
        clean = fear_readings([*_chain(NEAR), *_chain(FAR)], ASOF)
        split = fear_readings([*_chain(NEAR, basis_gap=8.0), *_chain(FAR, basis_gap=8.0)], ASOF)
        lopsided = fear_readings(
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
        # The average is the whole reason the level survives the source's
        # disagreement, so a chain missing one side at the nearest strike has to be
        # read one strike further rather than reported from a single side.
        unpaired = {UNDERLYING, UNDERLYING * 1.025}
        half_quoted = [
            quote
            for quote in _chain(NEAR)
            if not (quote.strike in unpaired and quote.put_call == CALL)
        ]

        readings = fear_readings([*half_quoted, *_chain(FAR)], ASOF)
        put_only = fear_readings(
            [quote for quote in (*_chain(NEAR), *_chain(FAR)) if quote.put_call == PUT], ASOF
        )

        # 0.975 is now the nearest rung quoting both, so the near leg reads 25.0 rather
        # than the 20.0 a put-only read of the money would have given.
        weight = (44 - 30) / (44 - 16)
        expected = (weight * _LADDER[0.975] ** 2 + (1 - weight) * _LADDER[1.00] ** 2) ** 0.5
        assert readings.iv_30d is not None
        self.assertAlmostEqual(readings.iv_30d, expected, places=9)
        self.assertEqual(put_only, FearReadings())

    def test_the_skew_is_the_downside_put_minus_the_at_the_money_put(self) -> None:
        readings = fear_readings([*_chain(NEAR), *_chain(FAR)], ASOF)

        self.assertEqual(readings.iv_skew, 10.0)

    def test_a_uniform_disagreement_moves_both_skew_legs_together(self) -> None:
        # Both legs come from the put side, so a disagreement of the same size at every
        # strike lifts them equally and leaves the difference alone. A leg taken off
        # the call side would instead carry the whole of it.
        clean = fear_readings([*_chain(NEAR), *_chain(FAR)], ASOF)
        skewed = fear_readings([*_chain(NEAR, basis_gap=10.0), *_chain(FAR, basis_gap=10.0)], ASOF)

        self.assertEqual(skewed.iv_skew, clean.iv_skew)

    def test_a_disagreement_that_differs_across_strikes_does_move_the_skew(self) -> None:
        # This is why the skew is gated at all: the price the source quoted against
        # also picks which strikes the two legs land on, so an uneven error separates
        # them. Below the bound the movement is smaller than the reading's own noise.
        uneven = {0.95: 8.0}
        clean = fear_readings([*_chain(NEAR), *_chain(FAR)], ASOF)
        skewed = fear_readings([*_chain(NEAR, gaps=uneven), *_chain(FAR, gaps=uneven)], ASOF)

        assert clean.iv_skew is not None
        assert skewed.iv_skew is not None
        self.assertAlmostEqual(skewed.iv_skew - clean.iv_skew, 4.0, places=9)

    def test_sides_that_disagree_beyond_the_bound_withhold_the_differences(self) -> None:
        # Past the bound the chain cannot be trusted to subtract one volatility from
        # another, but the level still averages the error away and is kept.
        ladder = {0.90: 50.0, 0.95: 46.0, 0.975: 42.0, 1.00: 40.0, 1.025: 38.0, 1.05: 36.0}

        readings = fear_readings(
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
        # Small disagreements are routine in the source; refusing them would empty
        # the series for a condition that changes nothing.
        ladder = {0.90: 50.0, 0.95: 46.0, 0.975: 42.0, 1.00: 40.0, 1.025: 38.0, 1.05: 36.0}

        readings = fear_readings(
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

    def test_calls_quoted_above_puts_are_refused_the_same_as_below(self) -> None:
        # The basis error runs both ways in the source, so the check has to read the
        # size of the disagreement rather than its direction.
        ladder = {0.90: 50.0, 0.95: 46.0, 0.975: 42.0, 1.00: 40.0, 1.025: 38.0, 1.05: 36.0}
        inverted = {0.95: -25.0, 0.975: -25.0, 1.025: -25.0, 1.05: -25.0}

        readings = fear_readings(
            [
                *_chain(NEAR, ladder=ladder, gaps=inverted),
                *_chain(FAR, ladder=ladder, gaps=inverted),
            ],
            ASOF,
        )

        self.assertIsNone(readings.iv_skew)

    def test_a_contract_the_source_left_unquoted_is_not_read_as_one_percent(self) -> None:
        # The source writes a placeholder volatility rather than a blank on contracts
        # deep enough in the money to carry no time value. Reading it as a real 1%
        # would drag the at-the-money average and the basis gap it entered.
        placeholders = [
            OptionQuote(
                put_call=side,
                strike=UNDERLYING,
                expiry=expiry,
                implied_volatility=1.0,
                underlying=UNDERLYING,
            )
            for side in (PUT, CALL)
            for expiry in (NEAR, FAR)
        ]
        without = fear_readings([*_chain(NEAR), *_chain(FAR)], ASOF)

        readings = fear_readings([*placeholders, *_chain(NEAR), *_chain(FAR)], ASOF)

        self.assertEqual(readings, without)

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
        # The two expiries straddle the target unevenly, so a weight that reads the
        # wrong side of the bracket lands on a different number.
        self.assertNotEqual((NEAR - ASOF).days + (WIDE - ASOF).days, 2 * 30)

        readings = fear_readings([*_chain(NEAR), *_chain(WIDE, level=20.0)], ASOF)

        assert readings.iv_30d is not None
        weight = (58 - 30) / (58 - 16)
        expected = (weight * 20.0**2 + (1 - weight) * 40.0**2) ** 0.5
        self.assertAlmostEqual(readings.iv_30d, expected, places=9)
        # Interpolating the volatilities directly would put it here instead.
        self.assertGreater(readings.iv_30d, weight * 20.0 + (1 - weight) * 40.0)

    def test_the_term_reading_uses_the_next_expiry_not_the_last_one(self) -> None:
        # A chain carries two dozen expiries, so reaching past the second one would
        # quote a contract months away as if it were next month.
        readings = fear_readings(
            [*_chain(NEAR), *_chain(FAR, level=9.0), *_chain(WIDE, level=25.0)], ASOF
        )

        self.assertEqual(readings.iv_term, 9.0)

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

    def test_the_skew_is_interpolated_to_the_target_maturity(self) -> None:
        # A skew read off whichever expiry happens to be in front is a different
        # quantity every week of the settlement cycle, and a reader comparing it to a
        # pooled quantile would find fear on the calendar. The two expiries here
        # carry different asymmetries, so the weight is visible in the answer:
        # bracketing from the wrong side, or in the wrong direction, lands elsewhere.
        self.assertEqual(((NEAR - ASOF).days, (WIDE - ASOF).days), (16, 58))

        readings = fear_readings([*_chain(NEAR, ladder=_STEEP), *_chain(WIDE, ladder=_FLAT)], ASOF)

        weight = (30**-0.5 - 58**-0.5) / (16**-0.5 - 58**-0.5)
        assert readings.iv_skew is not None
        self.assertAlmostEqual(readings.iv_skew, weight * 12.0 + (1 - weight) * 3.0, places=9)
        self.assertNotEqual(readings.iv_skew, 12.0)

    def test_an_expiry_landing_on_the_target_is_inside_the_bracket(self) -> None:
        # 30 days is the target itself, so an expiry sitting exactly on it brackets
        # the target rather than missing it — and answers without interpolating at
        # all, which is the one case where the reading is the contract's own number.
        self.assertEqual((ON_TARGET - ASOF).days, 30)

        leading = fear_readings(
            [*_chain(ON_TARGET, ladder=_STEEP), *_chain(FAR, ladder=_FLAT)], ASOF
        )
        trailing = fear_readings(
            [*_chain(NEAR, ladder=_STEEP), *_chain(ON_TARGET, ladder=_FLAT)], ASOF
        )

        self.assertEqual((leading.iv_skew, leading.iv_30d), (12.0, 20.0))
        self.assertEqual((trailing.iv_skew, trailing.iv_30d), (3.0, 23.0))

    def test_a_chain_with_one_priced_expiry_answers_nothing(self) -> None:
        # Every reading needs two expiries: the differences by definition, and the
        # level because one expiry past the target would have to be reported as a
        # 30-day contract the chain never priced. Reaching for a second expiry that is
        # not there must decline rather than fail the whole day.
        far_out = fear_readings(list(_chain(WIDE)), ASOF)
        on_target = fear_readings(list(_chain(ON_TARGET)), ASOF)
        too_near = fear_readings(list(_chain(NEAR)), ASOF)

        self.assertEqual(far_out, FearReadings())
        self.assertEqual(on_target, FearReadings())
        self.assertEqual(too_near, FearReadings())

    def test_the_basis_check_reaches_the_second_expiry_the_differences_read(self) -> None:
        # Both differences are built from the second expiry as well as the first, so a
        # check that only saw the front would pass a chain whose second month is the
        # broken one — and a term reading built on it can invert its own sign.
        ladder = {0.90: 50.0, 0.95: 46.0, 0.975: 42.0, 1.00: 40.0, 1.025: 38.0, 1.05: 36.0}

        clean = fear_readings(
            [*_chain(NEAR, ladder=ladder), *_chain(FAR, ladder=ladder, level=4.0)], ASOF
        )
        far_broken = fear_readings(
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
        clean = fear_readings([*_chain(NEAR), *_chain(FAR)], ASOF)

        readings = fear_readings([*slipped, *_chain(NEAR), *_chain(FAR)], ASOF)

        self.assertEqual(readings, clean)

    def test_the_basis_bound_admits_the_gap_it_names_and_refuses_the_next_one(self) -> None:
        # The bound sits above the range the source's own behaviour covers, so a
        # chain landing on it is still that behaviour. Moving it either way changes
        # which sessions keep their differences.
        ladder = {0.90: 50.0, 0.95: 46.0, 0.975: 42.0, 1.00: 40.0, 1.025: 38.0, 1.05: 36.0}

        def readings_at(gap: float) -> FearReadings:
            return fear_readings(
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

        readings = fear_readings(
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

        def readings_for(ladder: dict[float, float], gaps: dict[float, float]) -> FearReadings:
            return fear_readings(
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
        clean = fear_readings([*_chain(NEAR), *_chain(FAR)], ASOF)

        readings = fear_readings([*broken, *_chain(NEAR), *_chain(FAR)], ASOF)

        self.assertEqual(readings, clean)

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

    def test_a_number_the_source_could_not_produce_is_dropped(self) -> None:
        # A missing numeric arrives from the frame as NaN rather than as an absent
        # key, and NaN compares false against every bound, so it would pass each
        # later check and land in an average as a value that is not one.
        rows = [self._record(IV=float("nan")), self._record(UnderPx=float("inf"))]

        self.assertEqual(quotes_from_records(rows, ASOF), [])

    def test_an_expiry_carrying_a_time_is_read_as_its_date(self) -> None:
        # Typed as text the column carries the time with it, and date parsing refuses
        # the whole string rather than reading the part it understands.
        rows = [self._record(SQD="2026-08-14T00:00:00")]

        quotes = quotes_from_records(rows, ASOF)

        self.assertEqual([quote.expiry for quote in quotes], [date(2026, 8, 14)])

    def test_a_timestamp_expiry_becomes_a_date_the_maturity_can_subtract(self) -> None:
        # A timestamp passes an isinstance check against date but cannot be
        # subtracted from one, which would surface as a crash in the maturity
        # arithmetic rather than as an unreadable row here.
        rows = [self._record(SQD=datetime(2026, 8, 14, 15, 0, tzinfo=ZoneInfo("Asia/Tokyo")))]

        quotes = quotes_from_records(rows, ASOF)

        self.assertEqual([quote.expiry for quote in quotes], [date(2026, 8, 14)])
        self.assertEqual((quotes[0].expiry - ASOF).days, 16)


class SampleWindowTest(unittest.TestCase):
    """The step that draws the quantiles the readings are quoted against."""

    def test_a_step_that_divides_the_trading_week_is_refused(self) -> None:
        # Five business days is one calendar week, so the sample would land on one
        # weekday forever and could not be checked for the bias that carries.
        with self.assertRaises(ValueError):
            list(business_days(date(2018, 1, 1), date(2018, 3, 1), 5))
        with self.assertRaises(ValueError):
            list(business_days(date(2018, 1, 1), date(2018, 3, 1), 10))

    def test_a_step_beside_the_week_reaches_every_weekday(self) -> None:
        days = list(business_days(date(2018, 1, 1), date(2018, 12, 31), 4))

        self.assertEqual({day.weekday() for day in days}, {0, 1, 2, 3, 4})
        self.assertTrue(all(day.weekday() < 5 for day in days))

    def test_every_quantile_is_an_observed_reading(self) -> None:
        # The report quotes these numbers directly, and a reading that was averaged
        # or extrapolated into existence would be presented as one the market made.
        values = [float(n) for n in range(1, 101)]

        stats = quantiles(values)

        self.assertEqual(set(stats), {"p05", "p10", "p25", "p50", "p75", "p90", "p95", "p99"})
        self.assertEqual(stats["p05"], 6.0)
        self.assertEqual(stats["p50"], 51.0)
        self.assertEqual(stats["p99"], 100.0)
        self.assertTrue(all(value in values for value in stats.values()))

    def test_a_short_series_stays_inside_its_own_tail(self) -> None:
        # The top quantile of a series shorter than a hundred readings still has to
        # be one of them: p99 of ten readings is the tenth, and of one is that one.
        self.assertEqual(quantiles([float(n) for n in range(10)])["p99"], 9.0)
        self.assertEqual(quantiles([7.0])["p99"], 7.0)

    def test_an_output_path_that_is_not_a_csv_is_refused_before_any_fetch(self) -> None:
        # The write lands after hours of fetching, so a mistyped path would truncate
        # whatever is there and throw away the run at the same time.
        self.assertEqual(
            sample_main(
                ["--start", "2026-07-01", "--end", "2026-07-02", "--out", "data/store.sqlite"]
            ),
            2,
        )

    def test_the_step_counts_business_days_not_calendar_days(self) -> None:
        # Counting calendar days would skip a different number of sessions depending
        # on where a weekend fell, so the sample would not be evenly spaced.
        days = list(business_days(date(2018, 1, 1), date(2018, 1, 31), 4))

        self.assertEqual(
            days,
            [
                date(2018, 1, 1),
                date(2018, 1, 5),
                date(2018, 1, 11),
                date(2018, 1, 17),
                date(2018, 1, 23),
                date(2018, 1, 29),
            ],
        )


class OptionProviderTest(unittest.TestCase):
    """The provider's defences, which nothing else exercises."""

    def _series(self, provider_series_id: str) -> SeriesDefinition:
        return SeriesDefinition(
            series_id=f"jp.{provider_series_id}",
            name=provider_series_id,
            category="volatility",
            geography="japan",
            frequency="daily",
            unit="percent",
            provider="jquants_options",
            provider_series_id=provider_series_id,
            source_id=f"test-{provider_series_id}",
            source_url="https://example.invalid/",
            notes="test",
        )

    def test_the_exception_a_rate_limit_actually_raises_is_retried(self) -> None:
        # The client retries the rate-limited statuses itself and, when its attempts
        # run out, raises RetryError with no response attached. Classifying on the
        # status code alone made the wait unreachable for the one case it exists for,
        # and a suite that built its own 429-shaped exception could not see that.
        self.assertIsNone(RetryError("max retries").response)
        self.assertTrue(jquants_options._is_retryable(RetryError("max retries")))
        self.assertTrue(jquants_options._is_retryable(RequestsConnectionError("reset")))
        self.assertTrue(jquants_options._is_retryable(Timeout("read timed out")))

    def test_a_rate_limited_call_is_retried_rather_than_ending_the_range(self) -> None:
        # A range long enough to be worth fetching is long enough to meet a 429, and
        # raising discards every day already fetched.
        response = SimpleNamespace(status_code=429)
        self.assertTrue(jquants_options._is_retryable(RuntimeError_with(response)))

    def test_an_error_that_will_not_change_is_not_retried(self) -> None:
        # Waiting out an auth or schema failure only delays the report.
        self.assertFalse(
            jquants_options._is_retryable(RuntimeError_with(SimpleNamespace(status_code=401)))
        )
        self.assertFalse(jquants_options._is_retryable(RuntimeError("no response attached")))

    def test_backoff_gives_up_and_names_the_day_without_leaking_the_key(self) -> None:
        attempts = []

        def always_limited(*, date_yyyymmdd: str) -> object:
            attempts.append(date_yyyymmdd)
            raise RetryError("max retries exceeded for key sekret")

        budget = jquants_options._BackoffBudget(jquants_options._BACKOFF_BUDGET_SECONDS)
        with (
            mock.patch.object(jquants_options.time, "sleep") as slept,
            self.assertRaises(IndicatorsProviderError) as caught,
        ):
            jquants_options._call_with_backoff(
                always_limited, date(2026, 7, 29), api_key="sekret", budget=budget
            )

        self.assertEqual(len(attempts), 1 + len(jquants_options._RATE_LIMIT_BACKOFF_SECONDS))
        self.assertEqual(
            [call.args[0] for call in slept.call_args_list],
            list(jquants_options._RATE_LIMIT_BACKOFF_SECONDS),
        )
        self.assertIn("2026-07-29", str(caught.exception))
        self.assertNotIn("sekret", str(caught.exception))

    def test_the_waiting_stops_once_the_fetch_has_spent_its_budget(self) -> None:
        # A range where every day meets the rate limit would otherwise wait 18.5
        # minutes per day and outlast the job that scheduled it — and a batch killed
        # from outside loses the whole day's publish, not just this series.
        def always_limited(*, date_yyyymmdd: str) -> object:
            raise RetryError("max retries")

        budget = jquants_options._BackoffBudget(100.0)
        with (
            mock.patch.object(jquants_options.time, "sleep") as slept,
            self.assertRaises(IndicatorsProviderError),
        ):
            jquants_options._call_with_backoff(
                always_limited, date(2026, 7, 29), api_key="k", budget=budget
            )

        # 30 and 60 fit inside 100; 120 does not, and the ladder ends there.
        self.assertEqual([call.args[0] for call in slept.call_args_list], [30, 60])
        self.assertEqual(budget.remaining_seconds, 10.0)

    def test_the_budget_is_shared_across_the_days_of_one_fetch(self) -> None:
        # Spending it per day would let a long range wait without any bound at all.
        budget = jquants_options._BackoffBudget(90.0)

        with mock.patch.object(jquants_options.time, "sleep"):
            self.assertTrue(budget.spend(60.0))
            self.assertFalse(budget.spend(60.0))
            self.assertTrue(budget.spend(30.0))

        self.assertEqual(budget.remaining_seconds, 0.0)

    def test_the_registry_bands_hold_every_reading_and_catch_an_inflated_one(self) -> None:
        # The service refuses observations outside the registry's plausible range for
        # every provider, so what this provider owes is bands that fit: wide enough
        # that 8.5 years of readings sit inside with room for a deeper crash, narrow
        # enough that a value inflated by a scale mistake lands outside. A band that
        # spans zero cannot catch the opposite mistake, and the two difference series
        # need one that does — their sign is the reading.
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

    def test_the_three_series_read_one_chain_per_day(self) -> None:
        # Without sharing, a ten-year backfill makes three times the calls it needs.
        fetched: list[date] = []

        def one_chain(*, date_yyyymmdd: str) -> object:
            fetched.append(
                date.fromisoformat(f"{date_yyyymmdd[:4]}-{date_yyyymmdd[4:6]}-{date_yyyymmdd[6:]}")
            )
            return _FakeFrame(_chain_records(fetched[-1]))

        provider = jquants_options.JQuantsOptionsProvider()
        client = SimpleNamespace(get_drv_bars_daily_opt_225=one_chain)
        with (
            mock.patch.object(jquants_options, "_client", return_value=client),
            mock.patch.object(jquants_options, "_read_api_key", return_value="k"),
        ):
            for provider_series_id in ("n225_iv_30d", "n225_iv_skew", "n225_iv_term"):
                provider.fetch(
                    self._series(provider_series_id),
                    start=date(2026, 7, 27),
                    end=date(2026, 7, 29),
                    session=cast(HttpSession, None),
                )

        self.assertEqual(len(fetched), 3)  # three weekdays, not nine
        self.assertEqual(sorted(set(fetched)), fetched)

    def test_a_weekend_is_never_requested(self) -> None:
        self.assertEqual(
            jquants_options._days(date(2026, 7, 31), date(2026, 8, 3)),
            [date(2026, 7, 31), date(2026, 8, 3)],
        )

    def test_a_single_day_range_asks_for_that_day(self) -> None:
        # The daily refresh asks for one day. A range that excluded its own end would
        # leave the series never advancing while reporting no failure at all.
        one_day = date(2026, 7, 29)

        self.assertEqual(jquants_options._days(one_day, one_day), [one_day])
        self.assertEqual(jquants_options._days(one_day, one_day - timedelta(days=1)), [])

    def test_a_server_side_failure_is_waited_out_and_a_refusal_is_not(self) -> None:
        # A backfill runs for hours against one endpoint; a gateway blinking has to
        # cost a wait rather than the whole range, while a refusal has to end it.
        for status in (429, 500, 502, 503, 504):
            with self.subTest(status=status):
                self.assertTrue(
                    jquants_options._is_retryable(
                        RuntimeError_with(SimpleNamespace(status_code=status))
                    )
                )
        for status in (400, 403, 404):
            with self.subTest(status=status):
                self.assertFalse(
                    jquants_options._is_retryable(
                        RuntimeError_with(SimpleNamespace(status_code=status))
                    )
                )

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

    def test_a_contract_the_chain_carries_twice_is_refused(self) -> None:
        # Every reading resolves a strike to one volatility through a dict, so a second
        # row for the same contract wins on arrival order with nothing downstream able
        # to see it — and the basis median does not move for one duplicated strike.
        rows = _chain_records()
        clean = list(rows)
        rows.append(dict(rows[0]))

        jquants_options._require_one_row_per_contract(clean, ASOF)

        with self.assertRaises(IndicatorsProviderError) as caught:
            jquants_options._require_one_row_per_contract(rows, ASOF)
        self.assertIn("same contract", str(caught.exception))

    def test_a_chain_answering_for_another_day_is_refused(self) -> None:
        # The observation is stamped with the requested date, so a session served the
        # previous day's chain would carry the previous day's fear under today's date.
        provider = jquants_options.JQuantsOptionsProvider()
        stale = SimpleNamespace(
            get_drv_bars_daily_opt_225=lambda **_: _FakeFrame(_chain_records(ASOF - timedelta(1)))
        )
        with (
            mock.patch.object(jquants_options, "_client", return_value=stale),
            mock.patch.object(jquants_options, "_read_api_key", return_value="k"),
            self.assertRaises(IndicatorsProviderError) as caught,
        ):
            provider.fetch(
                self._series("n225_iv_30d"),
                start=ASOF,
                end=ASOF,
                session=cast(HttpSession, None),
            )

        self.assertIn("2026-07-28", str(caught.exception))
        self.assertIn("2026-07-29", str(caught.exception))

    def test_a_row_whose_day_the_source_could_not_parse_does_not_escape(self) -> None:
        # The frame parses that column with errors coerced, so one bad row of ten
        # thousand arrives as a not-a-time sentinel. It passes the timestamp check and
        # its own date() is another sentinel, which sorts against nothing — the guard
        # has to read it as an absent day rather than raise past its own caller. The
        # real sentinel is used rather than an imitation: a stand-in that inherits
        # datetime's inequality is not the thing being defended against.
        import pandas

        self.assertIsInstance(pandas.NaT, datetime)
        self.assertIsNone(as_date(pandas.NaT))

        rows = _chain_records()
        rows[0]["Date"] = pandas.NaT

        jquants_options._require_requested_day(rows, ASOF)

        with self.assertRaises(IndicatorsProviderError):
            jquants_options._require_requested_day(rows, ASOF + timedelta(1))

    def test_the_backoff_budget_is_shared_by_every_series_of_one_run(self) -> None:
        # A pass is three series and the service retries each fetch, so a budget held
        # per fetch would bound six of them separately — long enough for the job that
        # scheduled it to be killed, which loses the whole day's publish.
        provider = jquants_options.JQuantsOptionsProvider()
        spent: list[float] = []

        def always_limited(**_: object) -> object:
            raise RetryError("max retries")

        client = SimpleNamespace(get_drv_bars_daily_opt_225=always_limited)
        with (
            mock.patch.object(jquants_options, "_client", return_value=client),
            mock.patch.object(jquants_options, "_read_api_key", return_value="k"),
            mock.patch.object(jquants_options.time, "sleep", side_effect=spent.append),
        ):
            for provider_series_id in ("n225_iv_30d", "n225_iv_skew", "n225_iv_term"):
                for _ in range(2):  # the service tries each fetch twice
                    with self.assertRaises(IndicatorsProviderError):
                        provider.fetch(
                            self._series(provider_series_id),
                            start=ASOF,
                            end=ASOF,
                            session=cast(HttpSession, None),
                        )

        self.assertLessEqual(sum(spent), jquants_options._BACKOFF_BUDGET_SECONDS)

    def test_a_day_the_chain_cannot_answer_produces_no_observation(self) -> None:
        # A closed market and a reading the chain could not compute are both facts
        # about the day, not failures; inventing a point for either would put a
        # number in the series that nothing observed.
        closed = date(2026, 7, 27)
        no_straddle = date(2026, 7, 28)  # both expiries past the target
        full = date(2026, 7, 29)

        def chain(*, date_yyyymmdd: str) -> object:
            day = date.fromisoformat(
                f"{date_yyyymmdd[:4]}-{date_yyyymmdd[4:6]}-{date_yyyymmdd[6:]}"
            )
            if day == closed:
                return _FakeFrame([])
            if day == no_straddle:
                return _FakeFrame(_records([*_chain(ON_TARGET), *_chain(WIDE, level=2.0)], day))
            return _FakeFrame(_chain_records(day))

        provider = jquants_options.JQuantsOptionsProvider()
        client = SimpleNamespace(get_drv_bars_daily_opt_225=chain)
        with (
            mock.patch.object(jquants_options, "_client", return_value=client),
            mock.patch.object(jquants_options, "_read_api_key", return_value="k"),
        ):
            observed = {
                provider_series_id: [
                    record.observed_at
                    for record in provider.fetch(
                        self._series(provider_series_id),
                        start=closed,
                        end=full,
                        session=cast(HttpSession, None),
                    )
                ]
                for provider_series_id in ("n225_iv_30d", "n225_iv_term")
            }

        # The 31-day/58-day pair brackets nothing at 30, so only the term survives it.
        self.assertEqual(observed["n225_iv_30d"], [full])
        self.assertEqual(observed["n225_iv_term"], [no_straddle, full])
