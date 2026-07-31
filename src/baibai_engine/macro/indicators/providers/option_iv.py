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
# is the source's own inconsistency rather than a market state. Routine gaps of a few
# points move the differences by less than the day-to-day noise in them, so the bound
# sits above the range the source stays inside rather than through it: it marks a chain
# that has left its own behaviour, and refusing every imperfect chain would empty the
# two difference readings for a condition that changes nothing.
MAX_BASIS_GAP = 20.0
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

    Each reading is independent: a chain that can price the front month but has no
    second one still yields `iv_30d` when the front month straddles the target.
    Returning None for the rest says the chain could not answer, which a zero would
    hide.

    `iv_30d` averages the put and the call at one strike, so a basis error that
    lifts one side and drops the other cancels and the level survives it. The other
    two subtract one volatility from another and do not cancel, so they are withheld
    once the chain fails the basis check.
    """
    by_expiry = _usable_expiries(quotes, asof)
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
    consistent = _basis_is_consistent(by_expiry[front])
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
    calendar rather than in the market. Quoting it at one maturity takes most of that
    out — a residue remains, since the bracket the interpolation spans is itself
    narrower some weeks than others — at the cost of the days where the two expiries
    do not straddle 30. Those are the same days the level cannot be quoted, so the two
    readings appear and disappear together.

    Linear in days rather than in variance: this is a difference between two
    volatilities, and a difference does not add over time the way a variance does.
    """
    if near_skew is None or far_days is None or far_skew is None or far_days == near_days:
        return None
    if not near_days <= TARGET_DAYS <= far_days:
        return None
    weight = (far_days - TARGET_DAYS) / (far_days - near_days)
    return weight * near_skew + (1 - weight) * far_skew


def _basis_is_consistent(quotes: Sequence[OptionQuote]) -> bool:
    """Whether the two sides of the chain are on one footing.

    A put and a call on one strike and expiry carry the same volatility, so the median
    gap across the strikes around the money is zero when both sides were computed
    consistently. What breaks that consistency in the source is not established here
    and the check does not need it: the gap is measurable, and a chain that shows a
    large one cannot support a reading built by subtracting one volatility from
    another, whatever the cause. A chain with no strike quoting both sides cannot be
    checked and is treated as unusable rather than assumed sound.
    """
    underlying = quotes[0].underlying
    low, high = underlying * (1 - BASIS_BAND), underlying * (1 + BASIS_BAND)
    sides: dict[float, dict[str, float]] = {}
    for quote in quotes:
        if low <= quote.strike <= high:
            sides.setdefault(quote.strike, {})[quote.put_call] = quote.implied_volatility
    gaps = [pair[PUT] - pair[CALL] for pair in sides.values() if PUT in pair and CALL in pair]
    if not gaps:
        return False
    return abs(_median(gaps)) <= MAX_BASIS_GAP


def _median(values: Sequence[float]) -> float:
    ordered = sorted(values)
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[middle]
    return (ordered[middle - 1] + ordered[middle]) / 2


def _usable_expiries(quotes: Sequence[OptionQuote], asof: date) -> dict[date, list[OptionQuote]]:
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
    market charges for movement itself.
    """
    underlying = quotes[0].underlying
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

    Both legs are puts. Reading the asymmetry off one side keeps the measure on a
    single volatility basis, so an error in the underlying price the source priced
    against shifts both legs together and leaves the difference alone. Taking the
    upside leg from the call side would put the whole basis error into the reading.
    """
    underlying = quotes[0].underlying
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
    """
    if far_days is None or far_volatility is None or far_days == near_days:
        return near_volatility if near_days >= TARGET_DAYS else None
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
        expiry = _as_date(record.get("SQD"))
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


def _as_date(value: object) -> date | None:
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
