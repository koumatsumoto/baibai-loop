"""Portfolio-wide time-weighted return from ledger replay and daily closes."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time
from decimal import Decimal
from math import prod
from zoneinfo import ZoneInfo

from baibai_engine.market.bars import JQuantsDailyBar
from baibai_engine.market.jpx_total_return import BenchmarkObservation
from baibai_engine.position.ledger import (
    ConfirmedTaxEvent,
    ContributionEvent,
    CostEvent,
    ExecutionEvent,
    IncomeEvent,
    LedgerEvent,
    MarketPrice,
    OpeningBalanceEvent,
    PortfolioLedgerDocument,
    PortfolioLedgerError,
    WithdrawalEvent,
    estimated_exit_tax_yen,
    replay_events_through,
    value_replayed_state,
)

_TOKYO = ZoneInfo("Asia/Tokyo")
_JPX_CLOSE = time(15, 30)


class PortfolioOutcomeError(ValueError):
    """Raised when a period return cannot be computed without approximation."""


@dataclass(frozen=True, slots=True)
class DailyNav:
    valuation_date: date
    nav_yen: int
    external_flow_yen: int = 0


@dataclass(frozen=True, slots=True)
class TimeWeightedReturn:
    period_start_date: date
    period_end_date: date
    cumulative_return_pct: float
    annualized_return_pct: float | None


@dataclass(frozen=True, slots=True)
class PortfolioOutcome:
    status: str
    reason: str | None
    horizon: str
    period_start_date: date
    period_end_date: date
    portfolio_twr_pct: float | None
    benchmark_cumulative_return_pct: float | None
    excess_percentage_points: float | None
    portfolio_annualized_return_pct: float | None
    benchmark_annualized_return_pct: float | None
    ending_cash_yen: int | None
    ending_reserved_cash_yen: int | None
    ending_holdings_market_value_yen: int | None
    confirmed_income_yen: int | None
    confirmed_cost_yen: int | None
    confirmed_tax_yen: int | None
    estimated_exit_tax_yen: int | None
    estimated_exit_tax_status: str
    open_tickers: tuple[str, ...]
    closed_tickers: tuple[str, ...]


def compute_time_weighted_return(
    observations: tuple[DailyNav, ...], *, horizon_years: int | None = None
) -> TimeWeightedReturn:
    """Chain close-to-close returns with contribution/withdrawal at day start."""

    if len(observations) < 2:
        raise PortfolioOutcomeError("insufficient_portfolio_history")
    if observations[0].nav_yen <= 0:
        raise PortfolioOutcomeError("zero_or_negative_nav")
    if observations[0].external_flow_yen:
        raise PortfolioOutcomeError("opening NAV must not carry an external flow")
    returns: list[float] = []
    previous = observations[0]
    for current in observations[1:]:
        if current.valuation_date <= previous.valuation_date:
            raise PortfolioOutcomeError("valuation dates must be strictly increasing")
        denominator = previous.nav_yen + current.external_flow_yen
        if denominator <= 0 or current.nav_yen <= 0:
            raise PortfolioOutcomeError("zero_or_negative_nav")
        returns.append(current.nav_yen / denominator - 1)
        previous = current
    cumulative = prod(1 + daily_return for daily_return in returns) - 1
    annualized = None if horizon_years is None else (1 + cumulative) ** (1 / horizon_years) - 1
    return TimeWeightedReturn(
        period_start_date=observations[0].valuation_date,
        period_end_date=observations[-1].valuation_date,
        cumulative_return_pct=cumulative * 100,
        annualized_return_pct=None if annualized is None else annualized * 100,
    )


def compute_portfolio_outcome(
    ledger: PortfolioLedgerDocument,
    benchmark: BenchmarkObservation,
    *,
    business_days: tuple[date, ...],
    bars: tuple[JQuantsDailyBar, ...],
) -> PortfolioOutcome:
    """Compute one official benchmark period without filling missing evidence.

    The result is structured as ``unresolved`` for expected data gaps. Ledger
    corruption still raises through the caller: it is an invalid input, not an
    uncertain investment outcome.
    """

    start, end = benchmark.period_start_date, benchmark.period_end_date
    if start not in business_days or end not in business_days:
        return _unresolved(benchmark, "basis_mismatch")
    valuation_days = tuple(day for day in business_days if start <= day <= end)
    if len(valuation_days) < 2:
        return _unresolved(benchmark, "insufficient_portfolio_history")
    opening = next(
        (event for event in ledger.events if isinstance(event, OpeningBalanceEvent)), None
    )
    if opening is None or opening.occurred_at > _close_instant(start):
        return _unresolved(benchmark, "insufficient_portfolio_history")

    bars_by_ticker_day = {(bar.ticker, bar.traded_at): bar for bar in bars}
    effective_event_dates = tuple(
        _effective_date_for_period(event.occurred_at, business_days, start=start, end=end)
        for event in ledger.events
    )
    navs: list[DailyNav] = []
    final_value = None
    for valuation_day in valuation_days:
        selected = tuple(
            event
            for event, effective_day in zip(ledger.events, effective_event_dates, strict=True)
            if effective_day is not None and effective_day <= valuation_day
        )
        state = replay_events_through(
            selected, _close_instant(valuation_day), require_expired_release=False
        )
        prices: dict[str, MarketPrice] = {}
        for ticker, lots in state.lots.items():
            if not any(lot.quantity > 0 for lot in lots):
                continue
            bar = bars_by_ticker_day.get((ticker, valuation_day))
            if bar is None:
                return _unresolved(benchmark, "missing_market_price")
            if bar.adjustment_factor not in (None, 0.0, 1.0):
                return _unresolved(benchmark, "corporate_action_unresolved")
            prices[ticker] = MarketPrice.model_validate(
                {
                    "ticker": ticker,
                    "price_yen": Decimal(str(bar.close)),
                    "observed_at": _close_instant(valuation_day).isoformat(),
                    "source_kind": "licensed_dataset",
                    "price_basis": "unadjusted_close",
                    "source_ref": (
                        f"market.sqlite:jquants_daily_bars:{ticker}:{valuation_day.isoformat()}"
                    ),
                }
            )
        try:
            value = value_replayed_state(state, prices)
        except PortfolioLedgerError:
            return _unresolved(benchmark, "missing_market_price")
        flow = _external_flow_for_day(ledger.events, effective_event_dates, valuation_day)
        navs.append(
            DailyNav(valuation_day, value.total_capital_yen, 0 if valuation_day == start else flow)
        )
        final_value = value

    assert final_value is not None
    try:
        twr = compute_time_weighted_return(
            tuple(navs), horizon_years=_horizon_years(benchmark.horizon)
        )
    except PortfolioOutcomeError as error:
        return _unresolved(benchmark, str(error))
    confirmed_income, confirmed_cost, confirmed_tax = _period_confirmed_cashflows(
        ledger.events, effective_event_dates, start, end
    )
    estimated_tax = estimated_exit_tax_yen(
        final_value.holdings,
        rate_bps=ledger.estimated_exit_tax_rate_bps,
        basis=ledger.estimated_exit_tax_basis,
    )
    open_tickers = tuple(sorted(holding.ticker for holding in final_value.holdings))
    closed_tickers = tuple(
        sorted(
            {
                event.ticker
                for event, effective_day in zip(ledger.events, effective_event_dates, strict=True)
                if isinstance(event, ExecutionEvent)
                and event.side == "sell"
                and effective_day is not None
                and start <= effective_day <= end
                and event.ticker not in open_tickers
            }
        )
    )
    return PortfolioOutcome(
        status="resolved",
        reason=None,
        horizon=benchmark.horizon,
        period_start_date=start,
        period_end_date=end,
        portfolio_twr_pct=twr.cumulative_return_pct,
        benchmark_cumulative_return_pct=benchmark.cumulative_return_pct,
        excess_percentage_points=twr.cumulative_return_pct - benchmark.cumulative_return_pct,
        portfolio_annualized_return_pct=twr.annualized_return_pct,
        benchmark_annualized_return_pct=benchmark.annualized_return_pct,
        ending_cash_yen=final_value.available_cash_yen,
        ending_reserved_cash_yen=final_value.reserved_cash_yen,
        ending_holdings_market_value_yen=final_value.holdings_market_value_yen,
        confirmed_income_yen=confirmed_income,
        confirmed_cost_yen=confirmed_cost,
        confirmed_tax_yen=confirmed_tax,
        estimated_exit_tax_yen=estimated_tax,
        estimated_exit_tax_status="estimated" if estimated_tax is not None else "unknown",
        open_tickers=open_tickers,
        closed_tickers=closed_tickers,
    )


def outcome_to_payload(outcome: PortfolioOutcome) -> dict[str, object]:
    return {
        "schema_version": 1,
        "kind": "portfolio_outcome",
        "status": outcome.status,
        "reason": outcome.reason,
        "horizon": outcome.horizon,
        "period_start_date": outcome.period_start_date.isoformat(),
        "period_end_date": outcome.period_end_date.isoformat(),
        "portfolio_twr_pct": outcome.portfolio_twr_pct,
        "benchmark_cumulative_return_pct": outcome.benchmark_cumulative_return_pct,
        "excess_percentage_points": outcome.excess_percentage_points,
        "portfolio_annualized_return_pct": outcome.portfolio_annualized_return_pct,
        "benchmark_annualized_return_pct": outcome.benchmark_annualized_return_pct,
        "ending_cash_yen": outcome.ending_cash_yen,
        "ending_reserved_cash_yen": outcome.ending_reserved_cash_yen,
        "ending_holdings_market_value_yen": outcome.ending_holdings_market_value_yen,
        "confirmed_income_yen": outcome.confirmed_income_yen,
        "confirmed_cost_yen": outcome.confirmed_cost_yen,
        "confirmed_tax_yen": outcome.confirmed_tax_yen,
        "estimated_exit_tax_yen": outcome.estimated_exit_tax_yen,
        "estimated_exit_tax_status": outcome.estimated_exit_tax_status,
        "open_tickers": list(outcome.open_tickers),
        "closed_tickers": list(outcome.closed_tickers),
    }


def _unresolved(benchmark: BenchmarkObservation, reason: str) -> PortfolioOutcome:
    return PortfolioOutcome(
        status="unresolved",
        reason=reason,
        horizon=benchmark.horizon,
        period_start_date=benchmark.period_start_date,
        period_end_date=benchmark.period_end_date,
        portfolio_twr_pct=None,
        benchmark_cumulative_return_pct=None,
        excess_percentage_points=None,
        portfolio_annualized_return_pct=None,
        benchmark_annualized_return_pct=None,
        ending_cash_yen=None,
        ending_reserved_cash_yen=None,
        ending_holdings_market_value_yen=None,
        confirmed_income_yen=None,
        confirmed_cost_yen=None,
        confirmed_tax_yen=None,
        estimated_exit_tax_yen=None,
        estimated_exit_tax_status="unknown",
        open_tickers=(),
        closed_tickers=(),
    )


def _tokyo_date(instant: datetime) -> date:
    return instant.astimezone(_TOKYO).date()


def _effective_date(instant: datetime, business_days: tuple[date, ...]) -> date | None:
    local = instant.astimezone(_TOKYO)
    event_day = local.date()
    if event_day in business_days and local.time() > _JPX_CLOSE:
        event_day = event_day.fromordinal(event_day.toordinal() + 1)
    return next((day for day in business_days if day >= event_day), None)


def _effective_date_for_period(
    instant: datetime, business_days: tuple[date, ...], *, start: date, end: date
) -> date | None:
    """Map only in-period events to JPX BOD; keep prior state distinct.

    Events before the requested start remain part of opening NAV but cannot be
    misclassified as a start-date cash flow or confirmed period cashflow. Events
    that roll beyond period end are intentionally omitted from this outcome.
    """

    local = instant.astimezone(_TOKYO)
    if local.date() < start:
        return local.date()
    effective = _effective_date(instant, business_days)
    return effective if effective is not None and effective <= end else None


def _close_instant(day: date) -> datetime:
    return datetime.combine(day, _JPX_CLOSE, tzinfo=_TOKYO)


def _external_flow_for_day(
    events: tuple[LedgerEvent, ...], effective_dates: tuple[date | None, ...], day: date
) -> int:
    total = 0
    for event, effective_day in zip(events, effective_dates, strict=True):
        if effective_day != day:
            continue
        if isinstance(event, ContributionEvent):
            total += event.amount_yen
        elif isinstance(event, WithdrawalEvent):
            total -= event.amount_yen
    return total


def _period_confirmed_cashflows(
    events: tuple[LedgerEvent, ...],
    effective_dates: tuple[date | None, ...],
    start: date,
    end: date,
) -> tuple[int, int, int]:
    income = cost = tax = 0
    for event, effective_day in zip(events, effective_dates, strict=True):
        if effective_day is None or not start <= effective_day <= end:
            continue
        if isinstance(event, IncomeEvent):
            income += event.amount_yen
        elif isinstance(event, CostEvent):
            cost += event.amount_yen
        elif isinstance(event, ConfirmedTaxEvent):
            tax += event.amount_yen
    return income, cost, tax


def _horizon_years(horizon: str) -> int:
    return {"1y": 1, "3y": 3, "5y": 5}[horizon]
