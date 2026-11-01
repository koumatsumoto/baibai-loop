"""Synthetic option chains shared by acquisition and offline-reading tests.

These controlled quotes describe fixture assumptions, not live market behavior.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date

from baibai_engine.machine_analysis.observations.economic_indicators.contracts import (
    SeriesDefinition,
)
from baibai_engine.machine_analysis.observations.economic_indicators.definitions import (
    load_definitions,
)
from baibai_engine.machine_analysis.observations.economic_indicators.option_iv import (
    CALL,
    PUT,
    SETTLEMENT_SNAPSHOT,
    OptionQuote,
)

ASOF = date(2026, 7, 29)
NEAR = date(2026, 8, 14)
FAR = date(2026, 9, 11)
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
