"""Interpret fixed Alpaca IEX daily aggregates without adopting a price for a decision."""

from __future__ import annotations

import re
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime, time
from decimal import Decimal
from typing import Literal
from zoneinfo import ZoneInfo

from baibai_engine.data_collection.providers.alpaca import (
    BarsCapture,
    read_complete_daily_bar_pages,
)
from baibai_engine.primitives.numbers import finite_decimal

_NEW_YORK = ZoneInfo("America/New_York")
_TIMESTAMP = re.compile(
    r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}"
    r"(?:\.(?P<fraction>[0-9]{1,9}))?(?:Z|[+-](?:[01][0-9]|2[0-3]):[0-5][0-9])"
)


@dataclass(frozen=True, slots=True)
class AlpacaBarSource:
    receipt_id: str
    body_sha256: str
    received_at: datetime
    json_pointer: str
    native_timestamp: str


@dataclass(frozen=True, slots=True)
class AlpacaDailyObservation:
    """Provider observations, not a proved listing, session close, or execution price."""

    returned_symbol: str
    requested_symbol: str | None
    symbol_asof: date | Literal["-"]
    adjustment: Literal["raw", "split"]
    trade_date: date
    bar_started_at: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal  # Provider-reported share quantity at this adjustment basis, not turnover.
    sources: tuple[AlpacaBarSource, ...]
    feed: Literal["iex"] = "iex"
    price_unit: Literal["USD_per_share"] = "USD_per_share"
    price_basis: Literal["vendor_daily_aggregate"] = "vendor_daily_aggregate"

    @property
    def alias_status(self) -> Literal["literal_requested_label", "unresolved_provider_mapping"]:
        if self.symbol_asof == "-":
            return "literal_requested_label"
        return "unresolved_provider_mapping"


def normalize_alpaca_daily_bars(capture: BarsCapture) -> tuple[AlpacaDailyObservation, ...]:
    """Project just the observed OHLCV; no market calendar, fill, or fixed-cutoff adoption."""
    query = capture.query
    rows: dict[tuple[str, date], tuple[AlpacaDailyObservation, list[AlpacaBarSource]]] = {}
    for page, groups in read_complete_daily_bar_pages(capture):
        for symbol, group in groups.items():
            # A dated asof changes the vendor's relabeling, not the proof of identity.
            requested_symbol = symbol if query.symbol_asof == "-" else None
            pointer = symbol.replace("~", "~0").replace("/", "~1")
            for index, fields in enumerate(group):
                native, started, trade_date = _daily_start(fields.get("t"))
                if not query.start <= trade_date <= query.end:
                    raise ValueError("Alpaca daily bar is outside the fixed Alpaca interval")
                if started > page.response.received_at:
                    raise ValueError("Alpaca daily bar starts after its receipt")
                o, h, low, c, volume = _ohlcv(fields)
                source = AlpacaBarSource(
                    page.receipt_id,
                    page.body_sha256,
                    page.response.received_at,
                    f"/bars/{pointer}/{index}",
                    native,
                )
                row = AlpacaDailyObservation(
                    symbol,
                    requested_symbol,
                    query.symbol_asof,
                    query.adjustment,
                    trade_date,
                    started,
                    o,
                    h,
                    low,
                    c,
                    volume,
                    (),
                )
                key = (symbol, trade_date)
                previous, references = rows.setdefault(key, (row, []))
                if previous != row:
                    raise ValueError(
                        "conflicting Alpaca daily observations for one symbol and date"
                    )
                references.append(source)
    return tuple(replace(row, sources=tuple(refs)) for row, refs in rows.values())


def _daily_start(value: object) -> tuple[str, datetime, date]:
    if (
        not isinstance(value, str)
        or (match := _TIMESTAMP.fullmatch(value)) is None
        or value.endswith("-00:00")
    ):
        raise ValueError("invalid Alpaca daily bar timestamp")
    try:
        started = datetime.fromisoformat(value).astimezone(UTC)
        local = started.astimezone(_NEW_YORK)
    except (ValueError, OverflowError) as exc:
        raise ValueError("invalid Alpaca daily bar timestamp") from exc
    # fromisoformat truncates beyond microseconds; do not admit sub-microsecond drift.
    if local.time() != time.min or (match["fraction"] or "").strip("0"):
        raise ValueError("Alpaca daily bar timestamp is not New York midnight")
    return value, started, local.date()


def _ohlcv(row: dict[str, object]) -> tuple[Decimal, Decimal, Decimal, Decimal, Decimal]:
    def number(key: str) -> Decimal:
        raw = row.get(key)
        if type(raw) not in (int, Decimal):
            raise ValueError(f"invalid Alpaca OHLCV {key}: expected a JSON number")
        return finite_decimal(raw)

    o, h, low, c, volume = (number(key) for key in ("o", "h", "l", "c", "v"))
    if not 0 < low <= min(o, c) <= max(o, c) <= h or volume < 0:
        raise ValueError("invalid Alpaca OHLCV bounds")
    return o, h, low, c, volume
