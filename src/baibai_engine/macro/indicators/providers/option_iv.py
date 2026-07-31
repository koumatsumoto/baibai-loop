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
from datetime import date

# A contract inside its last week prices the SQ auction rather than the market's
# view of the next month, so the near expiry is skipped once it gets that close.
MIN_DAYS_TO_EXPIRY = 7
# The constant-maturity target the readings are quoted at.
TARGET_DAYS = 30
# How far from at-the-money the skew wings sit. Moneyness rather than a fixed
# strike distance, so the measure means the same thing as the index level moves.
SKEW_PUT_MONEYNESS = 0.95
SKEW_CALL_MONEYNESS = 1.05
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
    second one still yields `iv_30d` when the front month straddles the target, and
    always yields `iv_skew`. Returning None for the rest says the chain could not
    answer, which a zero would hide.
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
    return FearReadings(
        iv_30d=_constant_maturity(
            near_days=(front - asof).days,
            near_volatility=front_volatility,
            far_days=(second - asof).days if second is not None else None,
            far_volatility=second_volatility,
        ),
        iv_skew=_skew(by_expiry[front]),
        iv_term=(second_volatility - front_volatility if second_volatility is not None else None),
    )


def _usable_expiries(quotes: Sequence[OptionQuote], asof: date) -> dict[date, list[OptionQuote]]:
    grouped: dict[date, list[OptionQuote]] = {}
    for quote in quotes:
        if (quote.expiry - asof).days < MIN_DAYS_TO_EXPIRY:
            continue
        if quote.implied_volatility <= 0 or quote.strike <= 0 or quote.underlying <= 0:
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
    """Downside protection minus upside, both a fixed distance out of the money.

    A market that fears a fall pays more for the put than for the equally distant
    call, so the gap is the asymmetry rather than the level.
    """
    underlying = quotes[0].underlying
    put = _nearest(quotes, PUT, underlying * SKEW_PUT_MONEYNESS)
    call = _nearest(quotes, CALL, underlying * SKEW_CALL_MONEYNESS)
    if put is None or call is None:
        return None
    return put - call


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
    if isinstance(value, date):
        return value
    text = str(value or "")[:10]
    try:
        return date.fromisoformat(text)
    except ValueError:
        return None
