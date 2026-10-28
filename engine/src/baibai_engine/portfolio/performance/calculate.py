"""Portfolio-wide time-weighted return from ledger replay and daily closes."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime, time
from decimal import Decimal
from math import prod
from zoneinfo import ZoneInfo

from baibai_engine.market.benchmark_observation import BenchmarkObservation
from baibai_engine.market.price_models import JQuantsDailyBar
from baibai_engine.portfolio.ledger.events import (
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
    """Raised when the declared daily-return convention lacks valid inputs."""


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
        # A positive opening NAV and the funded-base check below establish that
        # an unchanged zero continues a total loss already observed in this period.
        if previous.nav_yen == current.nav_yen == 0 and current.external_flow_yen == 0:
            previous = current
            continue
        # A positive funded base falling to zero is an observed -100% return.
        # A zero base (for example after a full withdrawal) is undefined instead.
        if denominator <= 0 or current.nav_yen < 0:
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
    confirm_price_basis: Callable[[str, date, date], bool] | None = None,
) -> PortfolioOutcome:
    """Compute one official benchmark period without filling missing evidence.

    The result is structured as ``unresolved`` for expected data gaps. Ledger
    corruption still raises through the caller: it is an invalid input, not an
    uncertain investment outcome. An optional read-only basis check can establish
    pre-period holding units without supplying otherwise unused historical closes.
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
    if opening is None or (
        opening.occurred_on > start
        if opening.occurred_on is not None
        else opening.occurred_at > _close_instant(start)
    ):
        return _unresolved(benchmark, "insufficient_portfolio_history")

    bars_by_ticker_day = {(bar.ticker, bar.traded_at): bar for bar in bars}
    effective_event_dates = tuple(
        _effective_date_for_period(event, business_days, start=start, end=end)
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
        # No expiry check here: this prefix ends wherever the valuation day falls, so a
        # reservation lapsing that session with its release reported that evening is the
        # ordinary shape of the ledger at this instant, not a ledger awaiting a human.
        # Date-only facts are selected by their reported day. Their internal ordering
        # keys may be after the close and must not impose a second clock-time filter.
        state = replay_events_through(
            selected, datetime.combine(valuation_day, time.max, tzinfo=_TOKYO)
        )
        prices: dict[str, MarketPrice] = {}
        for ticker, lots in state.lots.items():
            if not any(lot.quantity > 0 for lot in lots):
                continue
            bar = bars_by_ticker_day.get((ticker, valuation_day))
            if bar is None:
                return _unresolved(benchmark, "missing_market_price")
            prices[ticker] = MarketPrice.model_validate(
                {
                    "ticker": ticker,
                    "price_yen": Decimal(str(bar.close)),
                    "price_as_of": valuation_day,
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
    # Check the whole holding episode, including before the opening NAV and the
    # final sale day. Looking only at end-of-day holdings misses a fully sold name.
    # A purchase already reports quantities on its own session's unit basis.
    for ticker, acquired_on, through in _holding_basis_intervals(
        ledger.events, effective_event_dates, start=start, end=end
    ):
        if acquired_on == through:
            continue
        if confirm_price_basis is not None:
            confirmed = confirm_price_basis(ticker, acquired_on, through)
        else:
            # Supplied in-period bars cannot certify a pre-period quantity basis.
            days = tuple(day for day in business_days if acquired_on < day <= through)
            confirmed = (
                acquired_on >= start
                and bool(days)
                and days[-1] == through
                and all(
                    (bar := bars_by_ticker_day.get((ticker, day))) is not None
                    and bar.adjustment_factor == 1.0
                    for day in days
                )
            )
        if not confirmed:
            return _unresolved(benchmark, "corporate_action_unresolved")
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
                and start < effective_day <= end
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


def _holding_basis_intervals(
    events: tuple[LedgerEvent, ...],
    effective_dates: tuple[date | None, ...],
    *,
    start: date,
    end: date,
) -> tuple[tuple[str, date, date], ...]:
    """Unit-check intervals for reconciled positions relevant after opening close."""
    balances: dict[str, int] = {}
    acquired: dict[str, date] = {}
    intervals: list[tuple[str, date, date]] = []
    for event, effective in zip(events, effective_dates, strict=True):
        if not isinstance(event, ExecutionEvent) or effective is None or effective > end:
            continue
        ticker = event.ticker
        day = event.occurred_on or event.occurred_at.astimezone(_TOKYO).date()
        balance = balances.get(ticker, 0)
        if event.side == "buy":
            if balance == 0:
                acquired[ticker] = day
            balances[ticker] = balance + event.quantity
        else:
            balances[ticker] = balance - event.quantity
            if balances[ticker] == 0:
                first = acquired.pop(ticker)
                if effective > start:
                    intervals.append((ticker, first, day))
    intervals.extend((ticker, first, end) for ticker, first in acquired.items())
    return tuple(intervals)


def outcome_to_payload(outcome: PortfolioOutcome) -> dict[str, object]:
    return {
        "schema_version": 2,
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


def _effective_date(event: LedgerEvent, business_days: tuple[date, ...]) -> date | None:
    local = event.occurred_at.astimezone(_TOKYO)
    event_day = event.occurred_on or local.date()
    if event.occurred_on is None and event_day in business_days and local.time() > _JPX_CLOSE:
        event_day = event_day.fromordinal(event_day.toordinal() + 1)
    return next((day for day in business_days if day >= event_day), None)


def _effective_date_for_period(
    event: LedgerEvent, business_days: tuple[date, ...], *, start: date, end: date
) -> date | None:
    """Map only in-period events to JPX BOD; keep prior state distinct.

    Reported dates do not inherit a close cutoff from their internal ordering keys.
    Exact-time facts after close still roll to the next business day.
    Events before the requested start remain part of opening NAV but cannot be
    misclassified as a start-date cash flow or confirmed period cashflow. Events
    that roll beyond period end are intentionally omitted from this outcome.
    """

    event_day = event.occurred_on or event.occurred_at.astimezone(_TOKYO).date()
    if event_day < start:
        return event_day
    effective = _effective_date(event, business_days)
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
        if effective_day is None or not start < effective_day <= end:
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
