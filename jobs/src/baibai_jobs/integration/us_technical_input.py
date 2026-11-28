"""Admit a bounded, fixed US SIP series to the existing technical model, offline."""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from hashlib import sha256
from math import isfinite

from baibai_engine.data_collection.providers.alpaca import BarsCapture
from baibai_engine.data_collection.providers.alpaca_calendar import AlpacaCalendarReading
from baibai_engine.machine_analysis.generation.contracts import GeneratedModel
from baibai_engine.machine_analysis.models.technical_analysis.calculations import (
    TECHNICAL_CALCULATION_REVISION,
)
from baibai_engine.machine_analysis.models.technical_analysis.generation import (
    generate_technical_observations,
)
from baibai_engine.machine_analysis.models.technical_analysis.price_path import observe_price_path
from baibai_engine.machine_analysis.observations.prices.price_models import PriceBar
from baibai_engine.machine_analysis.observations.prices.price_observation import PriceCalendar
from baibai_engine.machine_analysis.observations.prices.us_sip_coverage import (
    AlpacaSIPDailyBatch,
    inspect_alpaca_sip_daily_batch,
)
from baibai_engine.machine_analysis.observations.stocks.listing_master_file import (
    decode_current_listing_master,
)
from baibai_engine.primitives.json import canonical_json
from baibai_jobs.integration.us_daily_plan import plan_us_technical_scope

_READ_SEMANTICS = "fixed_vendor_split_at_receipt_not_original_vintage"


@dataclass(frozen=True, slots=True)
class USTechnicalInput:
    listing_session: date
    price_session: date
    models: tuple[GeneratedModel, ...]


def _fixed_panel(
    captures: Iterable[BarsCapture],
    *,
    days: tuple[date, ...],
    first: Mapping[str, int],
    not_before: datetime,
    cutoff: datetime,
) -> dict[str, list[PriceBar]] | None:
    by_day = {day: index for index, day in enumerate(days)}
    bars: dict[str, list[PriceBar]] = {symbol: [] for symbol in first}
    seen: set[str] = set()
    for capture in captures:
        query = capture.query
        lo = by_day.get(query.start)
        if (
            query.feed != "sip"
            or query.adjustment != "split"
            or query.symbol_asof != "-"
            or lo is None
            or query.end != days[-1]
            or any(first.get(symbol) != lo or symbol in seen for symbol in query.symbols)
            or any(not not_before <= page.response.received_at <= cutoff for page in capture.pages)
        ):
            return None
        batch = AlpacaSIPDailyBatch(query, days[lo:])
        coverage = inspect_alpaca_sip_daily_batch(batch, capture)
        if coverage.failure is not None:
            return None
        seen.update(query.symbols)
        for row in coverage.observations:
            price = float(row.close)
            if not isfinite(price) or price <= 0:
                return None
            bars[row.returned_symbol].append(PriceBar(row.trade_date, price, None))
    # Only one terminal split capture per symbol: don't stitch adjustment
    # vintages. Missing prior observations are allowed; no stale current close.
    if any(not series or series[-1].traded_at != days[-1] for series in bars.values()):
        return None
    return bars


def admit_us_technical_input(
    *,
    listing_bytes: bytes,
    captured_listing_sha256: str,
    calendars: Iterable[AlpacaCalendarReading],
    captures: Iterable[BarsCapture],
    cutoff: datetime,
    last_admitted_session: date | None = None,
) -> USTechnicalInput | None:
    """Use trusted same-run capture metadata; do not interpret the one-day raw L3 packet."""
    if cutoff.utcoffset() is None:
        raise ValueError("US technical cutoff requires a timezone")
    if sha256(listing_bytes).hexdigest() != captured_listing_sha256:
        return None
    current = decode_current_listing_master(listing_bytes).get("US")
    scope = plan_us_technical_scope(current, calendars, cutoff=cutoff)
    if scope is None:
        return None
    session = scope.calendar.as_of
    if last_admitted_session is not None and session <= last_admitted_session:
        return None
    bars = _fixed_panel(
        captures,
        days=scope.calendar.trading_dates,
        first=scope.first,
        not_before=scope.not_before,
        cutoff=cutoff,
    )
    if bars is None:
        return None
    return USTechnicalInput(
        scope.listing_session,
        session,
        tuple(
            _technical_model(
                target.listing_key,
                target.instrument_id,
                bars[target.listing_key[1]],
                scope.calendar,
                scope.first[target.listing_key[1]],
            )
            for target in scope.targets
        ),
    )


def _technical_model(
    key: tuple[str, str],
    instrument_id: str,
    bars: Sequence[PriceBar],
    calendar: PriceCalendar,
    first: int,
) -> GeneratedModel:
    path = observe_price_path(
        bars, calendar=calendar, day_states={bar.traded_at: "priced" for bar in bars}
    ).payload
    path["row_state_basis"] = "alpaca_sip_split_daily_ohlcv"
    path["no_trade_reason"] = None
    # The vendor's adjustment vintage is not the target NY session.
    path["adjustment_as_of"] = None
    path["read_semantics"] = _READ_SEMANTICS
    payload = generate_technical_observations(
        bars, as_of=calendar.as_of, trading_dates=calendar.trading_dates, price_path=path
    )
    return GeneratedModel(
        model_id="technical_analysis",
        subject=instrument_id,
        calculation_revision=TECHNICAL_CALCULATION_REVISION,
        payload=json.loads(
            canonical_json(
                {
                    **payload,
                    "market": "US",
                    "primary_mic": key[0],
                    "ticker": key[1],
                    "price_source": "alpaca_sip_daily_split",
                    "price_unit": "USD_per_share",
                    "rsi_seed_scope": "retained_ny_sessions",
                    "read_semantics": _READ_SEMANTICS,
                    "history_scope_start": calendar.trading_dates[first],
                    "earlier_current_key_history": "unverified" if first else "outside_window",
                }
            )
        ),
    )
