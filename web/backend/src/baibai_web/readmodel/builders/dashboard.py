"""資産・損益・保有からdashboardを組み立てる。"""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Mapping
from datetime import datetime

from baibai_engine.machine_analysis.observations.jquants_decode import JQuantsProviderError
from baibai_engine.primitives.time import JST
from baibai_engine.storage.sqlite.source_cache.schema import SQLiteSchemaError
from baibai_web.readmodel.builders.presentation import (
    holding_view,
    percentage,
    reservation_last_valid_day,
)
from baibai_web.readmodel.models import (
    DashboardView,
    ReservationView,
    WarningView,
)
from baibai_web.sources.protocols import (
    LedgerSource,
    MarketSource,
)

_LOGGER = logging.getLogger(__name__)


def build_dashboard(
    ledger: LedgerSource,
    market: MarketSource,
) -> DashboardView:
    """Build the app's first view without performing storage I/O directly."""

    now = datetime.now(JST)
    today = now.date()

    try:
        portfolio = ledger.snapshot()
    except (ValueError, RuntimeError, sqlite3.Error) as error:
        return _empty_dashboard(
            generated_at=now,
            ledger_exists=True,
            ledger_error=str(error),
        )
    if portfolio is None:
        return _empty_dashboard(
            generated_at=now,
            ledger_exists=False,
            ledger_error=None,
        )

    snapshot = portfolio.snapshot
    security_names: Mapping[str, str] = {}
    if snapshot.holdings:
        try:
            security_names = market.security_names(as_of=today)
        except (JQuantsProviderError, SQLiteSchemaError, sqlite3.Error) as error:
            _LOGGER.warning("保有の銘柄名を取得できないためtickerで表示します: %s", error)
    holdings = [
        holding_view(
            holding,
            security_name=security_names.get(holding.ticker),
        )
        for holding in snapshot.holdings
    ]
    holdings.sort(
        key=lambda item: (item.market_value_yen is not None, item.market_value_yen or 0),
        reverse=True,
    )
    reservations = [
        ReservationView(
            reservation_id=item.reservation_id,
            side=item.side,
            ticker=item.ticker,
            sector=item.sector,
            remaining_quantity=item.remaining_quantity,
            price_guard_yen=str(item.price_guard_yen),
            reserved_yen=item.reserved_yen,
            expires_at=item.expires_at,
            expiry_label=item.expiry_label
            if item.expiry_label is not None
            else (
                reservation_last_valid_day(item.expires_at).isoformat()
                if item.expires_at is not None
                else None
            ),
            route_label=item.route_label,
        )
        for item in snapshot.active_reservations
    ]
    reservations.sort(key=lambda item: (item.expires_at is None, item.expires_at))
    warnings = [
        WarningView(
            code=item.code,
            scope=item.scope,
            key=item.key,
            actual_pct=item.actual_pct,
            warning_pct=item.warning_pct,
            overridden=item.overridden,
        )
        for item in snapshot.warnings
    ]
    holdings_market_value = snapshot.holdings_market_value_yen
    available_cash = snapshot.available_cash_yen
    reserved_cash = snapshot.reserved_cash_yen
    total = snapshot.total_capital_yen
    valuation_as_of = (
        None
        if any(item.market_price_as_of is None for item in holdings)
        else min(
            (item.market_price_as_of for item in holdings if item.market_price_as_of is not None),
            default=snapshot.as_of.astimezone(JST).date(),
        )
    )
    return DashboardView(
        generated_at=now,
        ledger_exists=True,
        ledger_error=None,
        ledger_as_of=snapshot.ledger_as_of,
        valuation_error=portfolio.valuation_error,
        valuation_as_of=valuation_as_of,
        total_capital_yen=total,
        available_cash_yen=available_cash,
        reserved_cash_yen=reserved_cash,
        holdings_market_value_yen=holdings_market_value,
        deployed_cost_yen=snapshot.deployed_cost_yen,
        realized_gross_pnl_yen=snapshot.realized_gross_pnl_yen,
        cash_pct=percentage(available_cash, total, digits=1),
        reserved_pct=percentage(reserved_cash, total, digits=1),
        deployed_pct=percentage(holdings_market_value, total, digits=1),
        holdings=holdings,
        reservations=reservations,
        warnings=warnings,
    )


def _empty_dashboard(
    *,
    generated_at: datetime,
    ledger_exists: bool,
    ledger_error: str | None,
) -> DashboardView:
    return DashboardView(
        generated_at=generated_at,
        ledger_exists=ledger_exists,
        ledger_error=ledger_error,
        ledger_as_of=None,
        valuation_error=None,
        valuation_as_of=None,
        total_capital_yen=None,
        available_cash_yen=None,
        reserved_cash_yen=None,
        holdings_market_value_yen=None,
        deployed_cost_yen=None,
        realized_gross_pnl_yen=None,
        cash_pct=None,
        reserved_pct=None,
        deployed_pct=None,
        holdings=[],
        reservations=[],
        warnings=[],
    )
