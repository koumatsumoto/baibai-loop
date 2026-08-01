"""Reduce a day's Nikkei 225 option chain to the three fear readings.

The chain is ten thousand rows a day and none of it is read directly, so it is
summarised at fetch time and only the summary is stored. Everything here is a
pure function over already-fetched rows: what the numbers mean has to be
checkable without a network call.

The readings answer "how expensive is protection, and how lopsided", which is a
different question from what the index level says. They are observations for a
human reading the macro context, not inputs to a mechanical timing rule.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime

# A contract inside its last week prices the SQ auction rather than the market's
# view of the next month, so the near expiry is skipped once it gets that close.
MIN_DAYS_TO_EXPIRY = 7
# The constant-maturity target the readings are quoted at.
TARGET_DAYS = 30
# How far below at-the-money the skew's downside leg sits. Moneyness rather than a
# fixed strike distance, so the measure means the same thing as the index level moves.
SKEW_PUT_MONEYNESS = 0.95
# The band the basis check reads, wide enough to cover the strikes any reading uses
# and narrow enough to stay out of the wings, where a thin quote moves the volatility
# by points for reasons that have nothing to do with the basis.
BASIS_BAND = 0.10
# Put-call parity puts the same volatility on both sides of a strike, so a gap at all
# is the source's own inconsistency rather than a market state. Two measurements put
# the bound here and they agree: the gap the source routinely stays inside reaches 12.4
# at its 99th percentile, and a skew compared against its own neighbouring sessions
# only starts to drift once the gap passes about 12. Below that, refusing a chain would
# empty the two difference readings for a condition that moves them by less than their
# own noise.
MAX_BASIS_GAP = 12.0
# A median over two strikes is the mean of two numbers, so one broken strike would set
# the verdict; the chains seen here carry twenty or more, and a day that does not is
# not a chain this check can speak about.
MIN_BASIS_PAIRS = 3
# What the source writes where it has no volatility to report. It is a value, not a
# blank, and it lands on contracts deep enough in the money to carry no time value —
# reading it as a 1% volatility would drag every average and gap it entered. Nothing
# in this market quotes a volatility this low, so the bound doubles as a floor.
PLACEHOLDER_VOLATILITY = 1.0
PUT = "1"
CALL = "2"


@dataclass(frozen=True, slots=True, kw_only=True)
class OptionQuote:
    """One contract's fields, already parsed out of the provider payload."""

    put_call: str
    strike: float
    expiry: date
    implied_volatility: float
    underlying: float


@dataclass(frozen=True, slots=True, kw_only=True)
class FearReadings:
    """One day's readings, in volatility points. None where the chain cannot say."""

    iv_30d: float | None = None
    iv_skew: float | None = None
    iv_term: float | None = None


def fear_readings(quotes: Sequence[OptionQuote], asof: date) -> FearReadings:
    """Summarise one day's chain.

    Returning None says the chain could not answer, which a zero would hide. `iv_30d`
    and `iv_skew` both need two expiries that bracket 30 days, so they appear and
    disappear together; `iv_term` needs only two expiries and is there on almost every
    session.

    `iv_30d` averages the put and the call at one strike, and at the money the two
    carry the same sensitivity to the price the source priced against, so an error in
    it moves them in opposite directions and the average survives. The other two are
    positions relative to that price — one subtracts two levels, the other selects the
    strikes it reads by moneyness — so both are withheld once the chain fails the
    basis check.
    """
    by_expiry = usable_expiries(quotes, asof)
    if not by_expiry:
        return FearReadings()
    expiries = sorted(by_expiry)
    atm = {expiry: _atm_volatility(by_expiry[expiry]) for expiry in expiries}
    priced = [expiry for expiry in expiries if atm[expiry] is not None]
    if not priced:
        return FearReadings()
    front = priced[0]
    front_volatility = atm[front]
    assert front_volatility is not None  # `priced` holds only the expiries it priced
    second = priced[1] if len(priced) > 1 else None
    second_volatility = atm[second] if second is not None else None
    near_days = (front - asof).days
    far_days = (second - asof).days if second is not None else None
    # Both difference readings are built from the second expiry as well as the first,
    # so checking only the front would pass a chain whose second month is the broken
    # one — and a term reading built on it can invert its own sign.
    consistent = _basis_is_consistent(by_expiry[front]) and (
        second is None or _basis_is_consistent(by_expiry[second])
    )
    return FearReadings(
        iv_30d=_constant_maturity(
            near_days=near_days,
            near_volatility=front_volatility,
            far_days=far_days,
            far_volatility=second_volatility,
        ),
        iv_skew=(
            _constant_maturity_skew(
                near_days=near_days,
                near_skew=_skew(by_expiry[front]),
                far_days=far_days,
                far_skew=_skew(by_expiry[second]) if second is not None else None,
            )
            if consistent
            else None
        ),
        iv_term=(
            second_volatility - front_volatility
            if consistent and second_volatility is not None
            else None
        ),
    )


def _constant_maturity_skew(
    *,
    near_days: int,
    near_skew: float | None,
    far_days: int | None,
    far_skew: float | None,
) -> float | None:
    """Interpolate the asymmetry to the same 30-day maturity the level is quoted at.

    The smile steepens as a contract approaches settlement, so a skew read off
    whichever expiry happens to be in front is a different quantity every week of the
    cycle, and a reader comparing it to a pooled quantile would find fear on the
    calendar rather than in the market. Quoting it at one maturity removes that, at the
    cost of the days where the two expiries do not straddle 30 — the same days the
    level cannot be quoted, so the two readings appear and disappear together.

    The skew falls away roughly as the inverse root of maturity, so the two readings
    are placed on that axis and the target read off the line between them. Walking the
    days instead bends a curve with a straight line, and the resulting bias is largest
    where the bracket is widest — which is most of the settlement cycle. Measured over
    the same sessions, the days-linear reading still runs 4.03 a week out against 3.67
    a month out (correlation -0.17 with the front maturity); this one runs
    3.49 / 3.56 / 3.60 / 3.63, which is no longer a gradient (-0.02).

    Interpolating along the axis rather than rescaling onto it keeps the answer a
    weighted average of the two readings, so it can never land outside them: the
    weight is 1 when the front expiry is the target and 0 when the second one is.
    """
    if near_skew is None or far_days is None or far_skew is None or far_days == near_days:
        return None
    if not near_days <= TARGET_DAYS <= far_days:
        return None
    near_root, far_root = near_days**-0.5, far_days**-0.5
    weight = (TARGET_DAYS**-0.5 - far_root) / (near_root - far_root)
    return float(weight * near_skew + (1 - weight) * far_skew)


def _basis_is_consistent(quotes: Sequence[OptionQuote]) -> bool:
    gap = basis_gap(quotes)
    return gap is not None and abs(gap) <= MAX_BASIS_GAP


def basis_gap(quotes: Sequence[OptionQuote]) -> float | None:
    """How far apart the two sides of one expiry are, in volatility points.

    A put and a call on one strike and expiry carry the same volatility, so this is
    zero when both sides were computed on one footing and its size measures how far the
    chain has left that. What breaks the footing in the source is not established here
    and the readings do not need it: the gap is measurable, and a chain showing a large
    one cannot support a reading positioned against the price it was quoted against,
    whatever the cause. None where too few strikes quote both sides to take a median
    over — such a chain cannot be checked at all, which is a different answer from a
    gap of zero.
    """
    underlying = _underlying(quotes)
    low, high = underlying * (1 - BASIS_BAND), underlying * (1 + BASIS_BAND)
    sides: dict[float, dict[str, float]] = {}
    for quote in quotes:
        if low <= quote.strike <= high:
            sides.setdefault(quote.strike, {})[quote.put_call] = quote.implied_volatility
    gaps = [pair[PUT] - pair[CALL] for pair in sides.values() if PUT in pair and CALL in pair]
    return _median(gaps) if len(gaps) >= MIN_BASIS_PAIRS else None


def _underlying(quotes: Sequence[OptionQuote]) -> float:
    """The price the expiry was quoted against.

    The source repeats it on every row of the chain, so any single row can be taken
    for it — and a row carrying a slipped decimal would then relocate the
    at-the-money strike, the basis band and both skew legs at once, while every
    resulting reading stayed inside its plausible bound. The median is what the rows
    agree on, and one wrong row cannot move it.
    """
    return _median([quote.underlying for quote in quotes])


def _median(values: Sequence[float]) -> float:
    ordered = sorted(values)
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[middle]
    return (ordered[middle - 1] + ordered[middle]) / 2


def usable_expiries(quotes: Sequence[OptionQuote], asof: date) -> dict[date, list[OptionQuote]]:
    grouped: dict[date, list[OptionQuote]] = {}
    for quote in quotes:
        if (quote.expiry - asof).days < MIN_DAYS_TO_EXPIRY:
            continue
        if quote.implied_volatility <= PLACEHOLDER_VOLATILITY:
            continue
        # The underlying is repeated on every row and every reading is a position
        # relative to it, so one row carrying nothing would put the basis band at
        # zero and empty the check. The strike needs no such guard: it is only ever
        # selected by distance from a positive underlying, so a row without one is
        # never the nearest.
        if quote.underlying <= 0:
            continue
        grouped.setdefault(quote.expiry, []).append(quote)
    return grouped


def _atm_volatility(quotes: Sequence[OptionQuote]) -> float | None:
    """The at-the-money reading, averaged across the put and the call.

    A single side carries its own directional bias — the put is bid for protection
    and the call is offered against holdings — so the average is closer to what the
    market charges for movement itself. It is also what survives the source's own
    inconsistency: at the money the two sides move a like amount for a given error in
    the price they were quoted against, so an error pushes them apart and leaves the
    average where it was. On the one sampled session whose sides disagreed by 20
    points the two legs read 46.5 and 28.0, and their average, 37.3, sat between the
    36.7 and 36.3 of the sessions either side of it.
    """
    underlying = _underlying(quotes)
    strikes = {quote.strike for quote in quotes}
    atm_strike = min(strikes, key=lambda strike: abs(strike - underlying))
    sides = {
        quote.put_call: quote.implied_volatility for quote in quotes if quote.strike == atm_strike
    }
    values = [sides[side] for side in (PUT, CALL) if side in sides]
    if not values:
        return None
    return sum(values) / len(values)


def _skew(quotes: Sequence[OptionQuote]) -> float | None:
    """How much more the market charges for a fall than for the same move at the money.

    Both legs are puts, which keeps the measure on one volatility basis: taking the
    upside leg from the call side would put the whole of the source's put-against-call
    inconsistency into the reading. It does not make the reading immune to that
    inconsistency, because the price the source quoted against also picks which
    strikes the two legs land on, and moving both along a curved smile does not leave
    their difference alone — which is why the basis check gates this reading too.
    """
    underlying = _underlying(quotes)
    wing = _nearest(quotes, PUT, underlying * SKEW_PUT_MONEYNESS)
    money = _nearest(quotes, PUT, underlying)
    if wing is None or money is None:
        return None
    return wing - money


def _nearest(quotes: Sequence[OptionQuote], put_call: str, target: float) -> float | None:
    side = [quote for quote in quotes if quote.put_call == put_call]
    if not side:
        return None
    return min(side, key=lambda quote: abs(quote.strike - target)).implied_volatility


def _constant_maturity(
    *,
    near_days: int,
    near_volatility: float,
    far_days: int | None,
    far_volatility: float | None,
) -> float | None:
    """Interpolate to a 30-day maturity in variance, not in volatility.

    Variance is what adds over time; interpolating the volatility directly would
    understate the reading whenever the two expiries disagree, and the disagreement
    is largest exactly when the market is frightened.

    Two expiries that bracket the target are required. A single expiry sitting past
    30 days would have to be reported as if it were a 30-day contract, which is a
    maturity the chain never priced; `opt_225` carries several expiries every day, so
    the chain answers or it does not.
    """
    if far_days is None or far_volatility is None or far_days == near_days:
        return None
    if not near_days <= TARGET_DAYS <= far_days:
        return None
    weight = (far_days - TARGET_DAYS) / (far_days - near_days)
    variance = weight * near_volatility**2 + (1 - weight) * far_volatility**2
    return float(variance**0.5)


def quotes_from_records(records: Sequence[Mapping[str, object]], asof: date) -> list[OptionQuote]:
    """Parse provider rows, dropping any the readings cannot use.

    Dropping rather than failing: a chain always carries contracts with no quote,
    and a day is still readable without them.
    """
    del asof
    quotes: list[OptionQuote] = []
    for record in records:
        expiry = as_date(record.get("SQD"))
        strike = _as_float(record.get("Strike"))
        volatility = _as_float(record.get("IV"))
        underlying = _as_float(record.get("UnderPx"))
        put_call = str(record.get("PCDiv") or "").strip()
        if expiry is None or strike is None or volatility is None or underlying is None:
            continue
        if put_call not in {PUT, CALL}:
            continue
        quotes.append(
            OptionQuote(
                put_call=put_call,
                strike=strike,
                expiry=expiry,
                implied_volatility=volatility,
                underlying=underlying,
            )
        )
    return quotes


def _as_float(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float | str):
        return None
    try:
        parsed = float(value)
    except ValueError:
        return None
    return parsed if parsed == parsed and abs(parsed) != float("inf") else None


def as_date(value: object) -> date | None:
    """Reduce whatever the payload carries to a plain date.

    A pandas Timestamp passes `isinstance(value, date)` but subtracting a date from
    one raises, so a payload that typed the column as a timestamp would fail deep in
    the maturity arithmetic instead of here. Rebuilding the date drops that path.
    """
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return date(value.year, value.month, value.day)
    text = str(value or "")[:10]
    try:
        return date.fromisoformat(text)
    except ValueError:
        return None
