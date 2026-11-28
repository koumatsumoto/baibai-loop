"""Compare Alpaca SIP daily observations with the qualified US symbol/session scope."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date
from itertools import pairwise
from typing import Literal

from baibai_engine.data_collection.providers.alpaca import (
    MAX_SYMBOLS,
    BarsCapture,
    BarsFailure,
    DailyBarsQuery,
)
from baibai_engine.machine_analysis.observations.prices.alpaca_normalization import (
    AlpacaDailyObservation,
    normalize_alpaca_daily_bars,
)
from baibai_engine.machine_analysis.observations.stocks.us_universe import RakutenUSCoverageRow

type SIPSymbolDay = tuple[str, date]
type SIPCoverageFailure = (
    BarsFailure | Literal["not_attempted", "wrong_query", "invalid_capture", "unexpected_session"]
)


@dataclass(frozen=True, slots=True)
class AlpacaSIPDailyBatch:
    query: DailyBarsQuery
    session_dates: tuple[date, ...]

    def __post_init__(self) -> None:
        if self.query.feed != "sip" or self.query.symbol_asof != "-":
            raise ValueError("SIP coverage requires an exact-symbol SIP query")
        days = self.session_dates
        if (
            not days
            or any(type(day) is not date for day in days)
            or any(earlier >= later for earlier, later in pairwise(days))
            or (days[0], days[-1]) != (self.query.start, self.query.end)
        ):
            raise ValueError("SIP coverage requires distinct US session dates matching the query")

    @property
    def expected(self) -> tuple[SIPSymbolDay, ...]:
        return tuple((symbol, day) for symbol in self.query.symbols for day in self.session_dates)


@dataclass(frozen=True, slots=True)
class AlpacaSIPDailyCoverage:
    batch: AlpacaSIPDailyBatch
    observations: tuple[AlpacaDailyObservation, ...]
    failure: SIPCoverageFailure | None

    @property
    def received(self) -> tuple[SIPSymbolDay, ...]:
        return tuple((row.returned_symbol, row.trade_date) for row in self.observations)

    @property
    def missing(self) -> tuple[SIPSymbolDay, ...]:
        if self.failure is not None:
            return ()
        received = set(self.received)
        return tuple(key for key in self.batch.expected if key not in received)

    @property
    def unverified(self) -> tuple[SIPSymbolDay, ...]:
        return self.batch.expected if self.failure is not None else ()


def plan_alpaca_sip_daily_batches(
    universe: Iterable[RakutenUSCoverageRow],
    *,
    session_dates: Iterable[date],
    adjustment: Literal["raw", "split"],
) -> tuple[AlpacaSIPDailyBatch, ...]:
    """Batch all eligible symbols; the caller supplies the applicable NY sessions."""
    days = tuple(session_dates)
    if not days or any(type(day) is not date for day in days):
        raise ValueError("SIP coverage requires distinct US session dates")
    days = tuple(sorted(days))
    symbols: set[str] = set()
    for row in universe:
        if row.state != "eligible":
            continue
        symbol = row.alpaca_symbol
        if row.venue not in ("XNAS", "XNYS") or symbol is None or symbol in symbols:
            raise ValueError("ambiguous or unqualified US symbol in SIP scope")
        symbols.add(symbol)
    if not symbols:
        raise ValueError("no eligible US symbols in SIP scope")
    ordered = tuple(sorted(symbols))
    return tuple(
        AlpacaSIPDailyBatch(
            DailyBarsQuery(
                symbols=ordered[offset : offset + MAX_SYMBOLS],
                start=days[0],
                end=days[-1],
                symbol_asof="-",
                adjustment=adjustment,
                feed="sip",
            ),
            days,
        )
        for offset in range(0, len(ordered), MAX_SYMBOLS)
    )


def inspect_alpaca_sip_daily_batch(
    batch: AlpacaSIPDailyBatch, capture: BarsCapture | None
) -> AlpacaSIPDailyCoverage:
    """Count validated observations, not trading-session finality or official closes.

    An incomplete or invalid receipt chain establishes neither presence nor absence.
    Its expected pairs stay unverified with a distinct failure, not confirmed missing.
    """
    if capture is None:
        return AlpacaSIPDailyCoverage(batch, (), "not_attempted")
    if capture.query != batch.query:
        return AlpacaSIPDailyCoverage(batch, (), "wrong_query")
    if not capture.complete:
        return AlpacaSIPDailyCoverage(batch, (), capture.failure or "invalid_capture")
    try:
        observations = normalize_alpaca_daily_bars(capture)
    except (ValueError, ArithmeticError):
        return AlpacaSIPDailyCoverage(batch, (), "invalid_capture")
    # Alpaca emits native daily bars only when every OHLCV field is nonzero.
    if batch.query.adjustment == "raw" and any(row.volume == 0 for row in observations):
        return AlpacaSIPDailyCoverage(batch, (), "invalid_capture")
    dates = set(batch.session_dates)
    if any(row.trade_date not in dates for row in observations):
        return AlpacaSIPDailyCoverage(batch, (), "unexpected_session")
    ordered = tuple(sorted(observations, key=lambda row: (row.returned_symbol, row.trade_date)))
    return AlpacaSIPDailyCoverage(batch, ordered, None)
