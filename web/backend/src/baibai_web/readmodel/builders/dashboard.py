"""保有・投資判断・イベントからdashboardを組み立てる。"""

from __future__ import annotations

from datetime import datetime, timedelta

from baibai_engine.read_api import PortfolioLedgerError
from baibai_web.readmodel.builders.presentation import (
    _JST,
    holding_view,
    latest_research_by_ticker,
    percentage,
    security_names_for_run,
)
from baibai_web.readmodel.models import (
    DashboardView,
    ReservationView,
    WarningView,
)
from baibai_web.sources.protocols import (
    LedgerSource,
    MarketPriceSource,
    ResearchSource,
    ScreeningSource,
)


def build_dashboard(
    ledger: LedgerSource,
    research: ResearchSource,
    screening: ScreeningSource,
    market: MarketPriceSource,
) -> DashboardView:
    """Build the app's first view without performing storage I/O directly."""

    now = datetime.now(_JST)
    today = now.date()
    revisions = research.revisions()
    latest_research = latest_research_by_ticker(revisions)
    latest_run = screening.latest_run()
    security_names = security_names_for_run(latest_run)
    research_load_errors = research.load_errors()

    if not ledger.exists():
        return _empty_dashboard(
            generated_at=now,
            ledger_exists=False,
            ledger_error=None,
            research_load_errors=research_load_errors,
        )
    try:
        snapshot = ledger.snapshot()
    except PortfolioLedgerError as error:
        return _empty_dashboard(
            generated_at=now,
            ledger_exists=True,
            ledger_error=str(error),
            research_load_errors=research_load_errors,
        )

    holding_tickers = [holding.ticker for holding in snapshot.holdings]
    earnings_dates = market.next_earnings_dates(holding_tickers, as_of=today)
    holdings = [
        holding_view(
            holding,
            revision=latest_research.get(holding.ticker),
            security_name=security_names.get(holding.ticker),
            next_earnings_date=earnings_dates.get(holding.ticker),
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
            ticker=item.ticker,
            sector=item.sector,
            remaining_quantity=item.remaining_quantity,
            price_guard_yen=str(item.price_guard_yen),
            reserved_yen=item.reserved_yen,
            expires_at=item.expires_at,
        )
        for item in snapshot.active_reservations
    ]
    reservations.sort(key=lambda item: item.expires_at)
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
            default=snapshot.as_of,
        )
    )
    return DashboardView(
        generated_at=now,
        ledger_exists=True,
        ledger_error=None,
        ledger_as_of=snapshot.as_of,
        ledger_stale=snapshot.as_of.date() <= today - timedelta(days=7),
        valuation_as_of=valuation_as_of,
        valuation_stale=valuation_as_of is None
        or valuation_as_of.date() <= today - timedelta(days=7),
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
        research_load_errors=research_load_errors,
    )


def _empty_dashboard(
    *,
    generated_at: datetime,
    ledger_exists: bool,
    ledger_error: str | None,
    research_load_errors: list[str],
) -> DashboardView:
    return DashboardView(
        generated_at=generated_at,
        ledger_exists=ledger_exists,
        ledger_error=ledger_error,
        ledger_as_of=None,
        ledger_stale=False,
        valuation_as_of=None,
        valuation_stale=False,
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
        research_load_errors=research_load_errors,
    )
