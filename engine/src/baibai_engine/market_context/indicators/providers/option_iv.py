"""Compute IV level, put skew and term difference from a Nikkei 225 option chain.

These pure functions consume provider rows without fetching or storing data.
The outputs are volatility points, not monetary insurance premiums or a direct
measurement of investor fear. They support interpretation, not trading signals.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime

# Chosen maturity policy: exclude contracts with fewer than seven days left.
# This limits near-expiry exposure; it does not prove all settlement effects vanish.
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
# Operational bound on the median put/call IV disagreement. This is a chosen
# tolerance, not a natural breakpoint or proof of a particular data error.
# Historical sample diagnostics are kept in the option-IV study report.
MAX_BASIS_GAP = 12.0
# A median over two strikes is the mean of two numbers, so one broken strike would set
# the verdict; the chains seen here carry twenty or more, and a day that does not is
# not a chain this check can speak about.
MIN_BASIS_PAIRS = 3
# Existing calculation policy, not a documented provider missing-value sentinel.
# A concentration at 1.0 does not prove missingness. Keep this threshold explicit
# until its effect is evaluated from retained originals; parsing preserves the value.
MIN_USABLE_VOLATILITY = 1.0
PUT = "1"
CALL = "2"
# The chain can carry a whole second copy of itself. On a session where the exchange
# called emergency margin, the source returns the settlement snapshot and the snapshot
# taken when the call was made, one row per contract each, telling them apart only by
# this field — and the two disagree on the price they were quoted against, so a reading
# that mixed them would carry a basis error the chain itself explains. 2016-08-02
# returns 4,380 rows, two per contract, the intervention copy carrying the previous
# session's underlying and a flat volatility. Only the settlement snapshot is read.
SETTLEMENT_SNAPSHOT = "002"


@dataclass(frozen=True, slots=True, kw_only=True)
class OptionQuote:
    """One contract's fields, already parsed out of the provider payload."""

    put_call: str
    strike: float
    expiry: date
    implied_volatility: float
    underlying: float


@dataclass(frozen=True, slots=True, kw_only=True)
class OptionIvReadings:
    """One day's readings, in volatility points. None where the chain cannot say."""

    iv_30d: float | None = None
    iv_skew: float | None = None
    iv_term: float | None = None


def option_iv_readings(quotes: Sequence[OptionQuote], asof: date) -> OptionIvReadings:
    """Summarise the two nearest usable, paired expiries.

    Level and skew require a bracket around 30 days. Skew also requires usable put
    legs and a passing basis check; term needs two priced expiries and the same
    basis check, but no 30-day bracket. Missing readings remain None, not zero.

    The level averages put/call volatility at the nearest paired strike and is
    deliberately retained even when the basis check fails. Symmetric errors cancel
    in that average; one-sided or asymmetric errors need not.
    """
    by_expiry = usable_expiries(quotes, asof)
    if not by_expiry:
        return OptionIvReadings()
    expiries = sorted(by_expiry)
    atm = {expiry: _atm_volatility(by_expiry[expiry]) for expiry in expiries}
    priced = [expiry for expiry in expiries if atm[expiry] is not None]
    if not priced:
        return OptionIvReadings()
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
    return OptionIvReadings(
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
    """Interpolate put skew on the inverse-square-root maturity axis.

    This is the indicator's chosen interpolation convention, not a guarantee that
    maturity effects or market noise disappear. Both expiries must bracket 30 days.
    The convex weights keep the result between the two available skew readings.
    Historical rationale and sample diagnostics are in
    reports/studies/2026-07-31-option-iv-quantiles/report.md.
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
    """Median put-minus-call implied volatility near the money, in points.

    Matching put/call IVs are expected under a common pricing basis and consistent
    prices. A nonzero gap is observable disagreement, not proof of its cause.
    This operational check withholds difference readings above the chosen bound.
    Too few paired strikes return None rather than claiming a zero gap.
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
    agree on, limiting the effect of an isolated outlier rather than guaranteeing
    immunity to every incorrect row.
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
        if quote.implied_volatility <= MIN_USABLE_VOLATILITY:
            continue
        # Nonpositive pricing coordinates cannot define a usable moneyness.
        # A zero strike can still be the nearest if other strikes are far away.
        if quote.underlying <= 0 or quote.strike <= 0:
            continue
        grouped.setdefault(quote.expiry, []).append(quote)
    return grouped


def _atm_volatility(quotes: Sequence[OptionQuote]) -> float | None:
    """Average put/call IV at the nearest strike quoting both sides.

    Symmetric opposite shifts cancel; asymmetric shifts can move the average.
    The level remains available even when the difference readings fail the basis
    check, so it must not be described as immune to source inconsistency.
    Reaching for the nearest paired strike can also move the reading away from
    exact at-the-money when the closest strike lacks one side.
    """
    underlying = _underlying(quotes)
    sides: dict[float, dict[str, float]] = {}
    for quote in quotes:
        sides.setdefault(quote.strike, {})[quote.put_call] = quote.implied_volatility
    paired = [strike for strike, pair in sides.items() if PUT in pair and CALL in pair]
    if not paired:
        return None
    # Resolve an exact distance tie toward the lower strike, not source row order.
    pair = sides[min(paired, key=lambda strike: (abs(strike - underlying), strike))]
    return (pair[PUT] + pair[CALL]) / 2


def _skew(quotes: Sequence[OptionQuote]) -> float | None:
    """Downside-put IV minus near-at-the-money put IV, in volatility points.

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
    return min(
        side, key=lambda quote: (abs(quote.strike - target), quote.strike)
    ).implied_volatility


def _constant_maturity(
    *,
    near_days: int,
    near_volatility: float,
    far_days: int | None,
    far_volatility: float | None,
) -> float | None:
    """Interpolate annualized variance levels to a 30-day maturity.

    This indicator weights the two squared volatility levels by calendar distance,
    then takes the square root. It does not interpolate total variance (variance
    multiplied by time to expiry), so time-additivity is not its justification.
    The result is at least the same-weight linear volatility average, but that
    algebraic difference alone does not establish forecast accuracy.

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


def settlement_rows(
    records: Sequence[Mapping[str, object]],
) -> list[Mapping[str, object]]:
    """The rows belonging to the settlement snapshot, and only those.

    Keeping the marked rows rather than dropping the marked-otherwise ones: a payload
    that stopped carrying the field would then quietly go back to reading both copies
    at once, which is the failure this exists to prevent. Losing the field instead
    empties the chain, and an empty chain is a day the readings decline to answer.
    """
    return [
        record
        for record in records
        if str(record.get("EmMrgnTrgDiv") or "").strip() == SETTLEMENT_SNAPSHOT
    ]


def quotes_from_records(records: Sequence[Mapping[str, object]]) -> list[OptionQuote]:
    """Parse provider rows, dropping any the readings cannot use.

    Dropping rather than failing: a chain always carries contracts with no quote,
    and a day is still readable without them.
    """
    quotes: list[OptionQuote] = []
    for record in settlement_rows(records):
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
    """Reduce whatever the payload carries to a plain date, or None where it carries none.

    A pandas Timestamp passes `isinstance(value, date)` but subtracting a date from
    one raises, so a payload that typed the column as a timestamp would fail deep in
    the maturity arithmetic instead of here. Rebuilding the date drops that path.

    The frame's date column is parsed with errors coerced, so a row the source left
    unparseable arrives as a not-a-time sentinel — which also passes the timestamp
    check, and whose own `date()` is another sentinel rather than a date. It compares
    unequal to itself, exactly as a NaN does, and that is what identifies it without
    naming the library that produced it.
    """
    if isinstance(value, datetime):
        return None if value != value else value.date()
    if isinstance(value, date):
        return date(value.year, value.month, value.day)
    text = str(value or "")[:10]
    try:
        return date.fromisoformat(text)
    except ValueError:
        return None
