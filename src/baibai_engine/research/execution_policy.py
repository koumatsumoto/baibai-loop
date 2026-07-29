"""Deterministic price and order proposals for human-operated equity execution."""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import ROUND_FLOOR, Decimal, InvalidOperation
from pathlib import Path
from typing import Annotated, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from baibai_engine.foundation.yaml_io import safe_load
from baibai_engine.position.ledger import PortfolioSnapshot
from baibai_engine.position.policy import PORTFOLIO_POLICY
from baibai_engine.research.thesis import (
    ThesisDocument,
    ThesisResult,
    thesis_core_hash,
)

_CONFIG = ConfigDict(frozen=True, strict=True, extra="forbid")
_TICKER = r"^[0-9A-Z]{4}$"
_CURRENT_SNAPSHOT_MAX_AGE = timedelta(minutes=5)


class ExecutionPolicyError(ValueError):
    """Raised when a policy proposal would invent price or execution facts."""


def _datetime(value: object) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError as error:
            raise ValueError("must be an ISO datetime") from error
    else:
        raise ValueError("must be an ISO datetime")
    if parsed.tzinfo is None:
        raise ValueError("must include a timezone")
    return parsed


def _date(value: object) -> date:
    if not isinstance(value, str):
        raise ValueError("must be an ISO date")
    try:
        return date.fromisoformat(value)
    except ValueError as error:
        raise ValueError("must be an ISO date") from error


def _decimal(value: object) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, int | float | str | Decimal):
        raise ValueError("must be a decimal number")
    if isinstance(value, str) and re.fullmatch(r"[0-9]+(?:\.[0-9]{1,4})?", value) is None:
        raise ValueError("decimal string must use fixed-point notation")
    try:
        parsed = Decimal(str(value))
    except InvalidOperation as error:
        raise ValueError("must be a decimal number") from error
    if not parsed.is_finite() or parsed <= 0:
        raise ValueError("must be a positive finite decimal number")
    exponent = parsed.as_tuple().exponent
    if not isinstance(exponent, int) or exponent < -4:
        raise ValueError("decimal number supports at most four decimal places")
    return parsed


def _tuple(value: object) -> object:
    return tuple(value) if isinstance(value, list) else value


class QuoteLevel(BaseModel):
    """One visible order-book level observed with the quote snapshot."""

    model_config = _CONFIG

    price_yen: Annotated[Decimal, Field(gt=0, decimal_places=4)]
    quantity: Annotated[int, Field(gt=0)]

    @field_validator("price_yen", mode="before")
    @classmethod
    def _parse_price(cls, value: object) -> Decimal:
        return _decimal(value)


class ExecutionQuote(BaseModel):
    """Provider-neutral quote and visible book evidence for one ticker."""

    model_config = _CONFIG

    ticker: Annotated[str, Field(pattern=_TICKER)]
    exchange: Annotated[str, Field(min_length=1)]
    observed_at: datetime
    session: Literal["regular", "pre_open", "after_hours", "closed"]
    source_kind: Literal["market_api", "official_exchange", "licensed_dataset", "test_fixture"]
    source_ref: Annotated[str, Field(min_length=1)]
    is_executable: bool
    freshness_status: Literal["current", "stale", "historical", "synthetic"]
    last_yen: Annotated[Decimal, Field(gt=0, decimal_places=4)]
    bid_yen: Annotated[Decimal, Field(gt=0, decimal_places=4)] | None
    ask_yen: Annotated[Decimal, Field(gt=0, decimal_places=4)] | None
    bid_size: Annotated[int, Field(gt=0)] | None
    ask_size: Annotated[int, Field(gt=0)] | None
    bid_depth: tuple[QuoteLevel, ...] = ()
    ask_depth: tuple[QuoteLevel, ...] = ()
    average_daily_value_yen: Annotated[int, Field(gt=0)]
    price_basis: Literal["realtime", "last_close_adjusted", "last_close_unadjusted"]
    basis_group_id: Annotated[str, Field(min_length=1)]
    tick_size_yen: Annotated[Decimal, Field(gt=0, decimal_places=4)]

    @field_validator("observed_at", mode="before")
    @classmethod
    def _parse_time(cls, value: object) -> datetime:
        return _datetime(value)

    @field_validator("last_yen", "bid_yen", "ask_yen", "tick_size_yen", mode="before")
    @classmethod
    def _parse_price(cls, value: object) -> Decimal | None:
        return None if value is None else _decimal(value)

    @field_validator("bid_depth", "ask_depth", mode="before")
    @classmethod
    def _parse_depth(cls, value: object) -> object:
        return _tuple(value)

    @model_validator(mode="after")
    def _quote_shape(self) -> ExecutionQuote:
        if (self.bid_yen is None) != (self.bid_size is None):
            raise ValueError("bid_yen and bid_size must be specified together")
        if (self.ask_yen is None) != (self.ask_size is None):
            raise ValueError("ask_yen and ask_size must be specified together")
        if self.bid_yen is not None and self.ask_yen is not None and self.bid_yen > self.ask_yen:
            raise ValueError("bid_yen cannot exceed ask_yen")
        for field, price in (
            ("last_yen", self.last_yen),
            ("bid_yen", self.bid_yen),
            ("ask_yen", self.ask_yen),
        ):
            if (
                price is not None
                and price / self.tick_size_yen != (price / self.tick_size_yen).to_integral_value()
            ):
                raise ValueError(f"{field} must align to tick_size_yen")
        if any(
            level.price_yen / self.tick_size_yen
            != (level.price_yen / self.tick_size_yen).to_integral_value()
            for level in (*self.bid_depth, *self.ask_depth)
        ):
            raise ValueError("quote depth prices must align to tick_size_yen")
        return self


class ExecutionPortfolioInput(BaseModel):
    """Portfolio facts already reconciled by the canonical ledger."""

    model_config = _CONFIG

    as_of: datetime
    available_cash_yen: Annotated[int, Field(ge=0)]
    reserved_cash_yen: Annotated[int, Field(ge=0)]
    total_capital_yen: Annotated[int, Field(gt=0)]
    dry_powder_floor_yen: Annotated[int, Field(ge=0)]
    board_lot: Annotated[int, Field(gt=0)]
    adv_participation_warning_pct: Annotated[float, Field(gt=0, le=100)]
    spread_warning_bps: Annotated[float, Field(gt=0, le=10_000)] | None
    ticker_exposure_yen: Annotated[int, Field(ge=0)]
    sector_exposure_yen: Annotated[int, Field(ge=0)]
    common_factor_exposure_yen: dict[
        Annotated[str, Field(min_length=1)], Annotated[int, Field(ge=0)]
    ]
    max_ticker_concentration_pct: Annotated[float, Field(gt=0, le=100)]
    max_sector_concentration_pct: Annotated[float, Field(gt=0, le=100)]
    max_common_factor_concentration_pct: Annotated[float, Field(gt=0, le=100)]
    existing_warning_codes: tuple[Annotated[str, Field(min_length=1)], ...] = ()

    @field_validator("existing_warning_codes", mode="before")
    @classmethod
    def _parse_warnings(cls, value: object) -> object:
        return _tuple(value)

    @field_validator("as_of", mode="before")
    @classmethod
    def _parse_as_of(cls, value: object) -> datetime:
        return _datetime(value)


class ExecutionPolicyInput(BaseModel):
    """Human-selectable policy knobs and observed execution inputs."""

    model_config = _CONFIG

    ticker: Annotated[str, Field(pattern=_TICKER)]
    sector: Annotated[str, Field(min_length=1)]
    common_factors: tuple[Annotated[str, Field(min_length=1)], ...]
    quantity: Annotated[int, Field(gt=0)]
    fill_priority: Literal["fill", "balanced", "price"]
    evaluated_at: datetime
    expires_at: datetime
    deep_discount_bps: Annotated[int, Field(ge=0, le=9_999)] | None
    quote: ExecutionQuote
    portfolio: ExecutionPortfolioInput

    @field_validator("evaluated_at", "expires_at", mode="before")
    @classmethod
    def _parse_expiry(cls, value: object) -> datetime:
        return _datetime(value)

    @field_validator("common_factors", mode="before")
    @classmethod
    def _parse_common_factors(cls, value: object) -> object:
        return _tuple(value)

    @model_validator(mode="after")
    def _input_shape(self) -> ExecutionPolicyInput:
        if self.quote.ticker != self.ticker:
            raise ValueError("quote ticker must match execution policy ticker")
        if self.quantity % self.portfolio.board_lot:
            raise ValueError("quantity must be a multiple of portfolio board_lot")
        if len(set(self.common_factors)) != len(self.common_factors):
            raise ValueError("common_factors must not contain duplicates")
        if self.expires_at <= self.evaluated_at:
            raise ValueError("expires_at must follow evaluation")
        return self


@dataclass(frozen=True, slots=True)
class ExecutionOption:
    tactic: Literal["buy_now", "shallow_limit", "deep_limit", "defer"]
    eligible: bool
    price_yen: Decimal | None
    quantity: int
    notional_yen: int | None
    reasons: tuple[str, ...]
    warnings: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ProposedOrder:
    tactic: Literal["buy_now", "shallow_limit", "deep_limit"]
    quantity: int
    limit_price_yen: Decimal
    expires_at: datetime


@dataclass(frozen=True, slots=True)
class ExecutionProposal:
    """A deterministic proposal for human review; it never submits an order."""

    ticker: str
    thesis_sha256: str
    max_acceptable_price_yen: Decimal
    required_5y_base_cagr_pct: float
    formula_version: Literal["five-year-base-cagr-v1"]
    recommended_tactic: Literal["buy_now", "shallow_limit", "deep_limit", "defer"]
    orders: tuple[ProposedOrder, ...]
    options: tuple[ExecutionOption, ...]
    spread_bps: float | None
    visible_ask_coverage: float | None
    adv_participation_pct: float | None
    cash_after_execution_yen: int
    quote_freshness_status: str
    quote_source_kind: str
    quote_observed_at: datetime
    evaluated_at: datetime
    ledger_as_of: datetime
    warnings: tuple[str, ...]
    prospective_concentration_warnings: tuple[str, ...]


def load_execution_policy_input(path: Path) -> ExecutionPolicyInput:
    """Load strict provider-neutral execution input from a YAML path."""

    try:
        raw = safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as error:
        raise ExecutionPolicyError(f"failed to load execution policy input: {error}") from error
    if not isinstance(raw, Mapping):
        raise ExecutionPolicyError("execution policy input root must be a mapping")
    try:
        return ExecutionPolicyInput.model_validate(raw)
    except ValidationError as error:
        raise ExecutionPolicyError(str(error)) from error


def evaluate_execution_policy(
    document: ThesisDocument,
    result: ThesisResult,
    policy_input: ExecutionPolicyInput,
) -> ExecutionProposal:
    """Compare four buy tactics without estimating fill probability or price direction."""

    if document.input_snapshot.ticker != policy_input.ticker:
        raise ExecutionPolicyError("thesis ticker must match execution policy ticker")
    if document.input_snapshot.sector != policy_input.sector:
        raise ExecutionPolicyError("execution policy sector must match the thesis")
    if document.input_snapshot.common_factors != policy_input.common_factors:
        raise ExecutionPolicyError("execution policy common factors must match the thesis")
    if result.decision_readiness != "ready":
        raise ExecutionPolicyError("execution policy requires a decision-ready thesis")
    if result.thesis_sha256 != thesis_core_hash(document):
        raise ExecutionPolicyError("execution policy result must match the thesis")
    if document.judgment.recommendation != "buy":
        raise ExecutionPolicyError("execution policy requires a buy recommendation")
    if policy_input.deep_discount_bps != document.estimates.deep_discount_bps:
        raise ExecutionPolicyError("execution policy deep discount must match the thesis")
    max_price = max_acceptable_price(document, tick_size_yen=policy_input.quote.tick_size_yen)
    quote = policy_input.quote
    portfolio = policy_input.portfolio
    evaluated_at = policy_input.evaluated_at
    ledger_age = evaluated_at - portfolio.as_of
    if ledger_age < timedelta() or ledger_age > _CURRENT_SNAPSHOT_MAX_AGE:
        raise ExecutionPolicyError("ledger snapshot must be current at evaluation")
    warnings = list(portfolio.existing_warning_codes)
    if quote.bid_yen is not None and quote.ask_yen is not None:
        spread_bps = float((quote.ask_yen - quote.bid_yen) / quote.ask_yen * 10_000)
        if portfolio.spread_warning_bps is not None and spread_bps > portfolio.spread_warning_bps:
            warnings.append("spread_exceeds_explicit_warning")
    else:
        spread_bps = None
        warnings.append("spread_unavailable")
    visible_ask_coverage = (
        quote.ask_size / policy_input.quantity if quote.ask_size is not None else None
    )
    if visible_ask_coverage is None:
        warnings.append("visible_ask_size_unavailable")
    elif visible_ask_coverage < 1:
        warnings.append("visible_ask_size_below_requested_quantity")
    actionable = _actionable_quote(
        quote,
        evaluated_at=evaluated_at,
        max_age=_CURRENT_SNAPSHOT_MAX_AGE,
    )
    if not actionable:
        warnings.append("quote_is_not_currently_actionable")
    all_visible_quotes_above_max = _all_visible_quotes_above_max(quote, max_price=max_price)
    if all_visible_quotes_above_max:
        warnings.append("all_visible_quotes_above_max_acceptable_price")

    buy_now = _buy_now_option(policy_input, max_price=max_price, actionable=actionable)
    shallow = _shallow_option(policy_input, max_price=max_price, actionable=actionable)
    deep = _deep_option(policy_input, shallow=shallow, actionable=actionable)
    options = (buy_now, shallow, deep, _defer_option())
    selected = _select_tactic(
        policy_input,
        buy_now=buy_now,
        shallow=shallow,
        deep=deep,
        quote_warnings=warnings,
        force_defer=all_visible_quotes_above_max,
    )
    orders = _proposed_orders(policy_input, selected=selected, shallow=shallow, deep=deep)
    order_notional = sum(_notional(order.limit_price_yen, order.quantity) for order in orders)
    cash_after = portfolio.available_cash_yen - order_notional
    if orders and cash_after < portfolio.dry_powder_floor_yen:
        raise ExecutionPolicyError("selected orders breach the dry-powder cash boundary")
    if orders:
        adv_participation = float(order_notional / quote.average_daily_value_yen * 100)
        if adv_participation > portfolio.adv_participation_warning_pct:
            warnings.append("adv_participation_exceeds_warning")
    else:
        adv_participation = None
    concentration_warnings = _prospective_concentration_warnings(policy_input, order_notional)
    warnings.extend(concentration_warnings)
    return ExecutionProposal(
        ticker=policy_input.ticker,
        thesis_sha256=result.thesis_sha256,
        max_acceptable_price_yen=max_price,
        required_5y_base_cagr_pct=document.estimates.required_5y_base_cagr_pct,
        formula_version="five-year-base-cagr-v1",
        recommended_tactic=selected,
        orders=orders,
        options=options,
        spread_bps=spread_bps,
        visible_ask_coverage=visible_ask_coverage,
        adv_participation_pct=adv_participation,
        cash_after_execution_yen=cash_after,
        quote_freshness_status=quote.freshness_status,
        quote_source_kind=quote.source_kind,
        quote_observed_at=quote.observed_at,
        evaluated_at=evaluated_at,
        ledger_as_of=portfolio.as_of,
        warnings=tuple(sorted(set(warnings))),
        prospective_concentration_warnings=concentration_warnings,
    )


def require_current_execution_input(policy_input: ExecutionPolicyInput, *, now: datetime) -> None:
    """Reject an execution input that is not current enough for a live CLI proposal."""

    evaluation_age = now - policy_input.evaluated_at
    if evaluation_age < timedelta() or evaluation_age > _CURRENT_SNAPSHOT_MAX_AGE:
        raise ExecutionPolicyError("execution input evaluation must be current for the CLI")
    if policy_input.expires_at <= now:
        raise ExecutionPolicyError("execution input has expired for the CLI")


def max_acceptable_price(document: ThesisDocument, *, tick_size_yen: Decimal) -> Decimal:
    """Derive the maximum entry price from the thesis's 5-year base scenario."""

    scenario = next(
        (
            item
            for item in document.estimates.scenarios
            if item.horizon_years == 5 and item.name == "base"
        ),
        None,
    )
    if scenario is None:
        raise ExecutionPolicyError("thesis has no 5y/base scenario")
    growth = Decimal(str(scenario.annual_earnings_growth_pct)) / 100
    share_change = Decimal(str(scenario.annual_share_count_change_pct)) / 100
    terminal_earnings = scenario.starting_earnings_yen * (Decimal(1) + growth) ** 5
    terminal_shares = scenario.starting_share_count * (Decimal(1) + share_change) ** 5
    if terminal_shares <= 0:
        raise ExecutionPolicyError("5y/base terminal share count must remain positive")
    terminal_price = terminal_earnings / terminal_shares * scenario.terminal_valuation_multiple
    terminal_total = terminal_price + scenario.cumulative_dividend_per_share_yen
    required_return = Decimal(str(document.estimates.required_5y_base_cagr_pct)) / 100
    raw_max_price = terminal_total / (Decimal(1) + required_return) ** 5
    rounded = (raw_max_price / tick_size_yen).to_integral_value(
        rounding=ROUND_FLOOR
    ) * tick_size_yen
    if rounded <= 0:
        raise ExecutionPolicyError("max acceptable price must remain positive after tick rounding")
    return rounded


def portfolio_input_from_snapshot(
    snapshot: PortfolioSnapshot,
    *,
    ticker: str,
    sector: str,
    common_factors: Sequence[str],
    board_lot: int = PORTFOLIO_POLICY["order_constraints"]["board_lot"],
    adv_participation_warning_pct: float = PORTFOLIO_POLICY["risk_budget"][
        "max_adv_participation_pct"
    ],
    spread_warning_bps: float | None = None,
) -> ExecutionPortfolioInput:
    """Adapt the canonical ledger snapshot without introducing another cash calculation."""

    dry_powder_pct = Decimal(str(PORTFOLIO_POLICY["cash_management"]["dry_powder_warning_pct"]))
    dry_powder_floor = int(
        (Decimal(snapshot.total_capital_yen) * dry_powder_pct / 100).to_integral_value(
            rounding=ROUND_FLOOR
        )
    )
    return ExecutionPortfolioInput(
        as_of=snapshot.as_of,
        available_cash_yen=snapshot.available_cash_yen,
        reserved_cash_yen=snapshot.reserved_cash_yen,
        total_capital_yen=snapshot.total_capital_yen,
        dry_powder_floor_yen=dry_powder_floor,
        board_lot=board_lot,
        adv_participation_warning_pct=adv_participation_warning_pct,
        spread_warning_bps=spread_warning_bps,
        ticker_exposure_yen=sum(
            holding.market_value_yen for holding in snapshot.holdings if holding.ticker == ticker
        )
        + sum(
            reservation.reserved_yen
            for reservation in snapshot.active_reservations
            if reservation.ticker == ticker
        ),
        sector_exposure_yen=sum(
            holding.market_value_yen for holding in snapshot.holdings if holding.sector == sector
        )
        + sum(
            reservation.reserved_yen
            for reservation in snapshot.active_reservations
            if reservation.sector == sector
        ),
        common_factor_exposure_yen={
            factor: sum(
                holding.market_value_yen
                for holding in snapshot.holdings
                if factor in holding.common_factors
            )
            + sum(
                reservation.reserved_yen
                for reservation in snapshot.active_reservations
                if factor in reservation.common_factors
            )
            for factor in common_factors
        },
        max_ticker_concentration_pct=PORTFOLIO_POLICY["risk_budget"][
            "max_ticker_concentration_pct"
        ],
        max_sector_concentration_pct=PORTFOLIO_POLICY["risk_budget"][
            "max_sector_concentration_pct"
        ],
        max_common_factor_concentration_pct=PORTFOLIO_POLICY["risk_budget"][
            "max_common_factor_concentration_pct"
        ],
        existing_warning_codes=tuple(sorted({warning.code for warning in snapshot.warnings})),
    )


def execution_proposal_to_payload(proposal: ExecutionProposal) -> dict[str, object]:
    """Serialize a policy result for the decision CLI's concise first layer."""

    return {
        "ticker": proposal.ticker,
        "thesis_sha256": proposal.thesis_sha256,
        "max_acceptable_price_yen": str(proposal.max_acceptable_price_yen),
        "required_5y_base_cagr_pct": proposal.required_5y_base_cagr_pct,
        "formula_version": proposal.formula_version,
        "evaluated_at": proposal.evaluated_at.isoformat(),
        "quote_observed_at": proposal.quote_observed_at.isoformat(),
        "ledger_as_of": proposal.ledger_as_of.isoformat(),
        "recommended_tactic": proposal.recommended_tactic,
        "orders": [
            {
                "tactic": order.tactic,
                "quantity": order.quantity,
                "limit_price_yen": str(order.limit_price_yen),
                "expires_at": order.expires_at.isoformat(),
            }
            for order in proposal.orders
        ],
        "cash_after_execution_yen": proposal.cash_after_execution_yen,
        # Warnings stay a complete list. There are at most a handful per proposal and
        # they carry no severity order, so any single-warning summary would have to
        # invent one — and picking the wrong one is worse than reading all of them.
        "warnings": list(proposal.warnings),
        "detail": {
            "options": [
                {
                    "tactic": option.tactic,
                    "eligible": option.eligible,
                    "price_yen": str(option.price_yen) if option.price_yen is not None else None,
                    "quantity": option.quantity,
                    "notional_yen": option.notional_yen,
                    "reasons": list(option.reasons),
                    "warnings": list(option.warnings),
                }
                for option in proposal.options
            ],
            "spread_bps": proposal.spread_bps,
            "visible_ask_coverage": proposal.visible_ask_coverage,
            "adv_participation_pct": proposal.adv_participation_pct,
            "quote": {
                "freshness_status": proposal.quote_freshness_status,
                "source_kind": proposal.quote_source_kind,
            },
            "prospective_concentration_warnings": list(proposal.prospective_concentration_warnings),
        },
    }


def _actionable_quote(quote: ExecutionQuote, *, evaluated_at: datetime, max_age: timedelta) -> bool:
    return (
        _quote_was_executable(quote)
        and quote.observed_at <= evaluated_at
        and evaluated_at - quote.observed_at <= max_age
    )


def _quote_was_executable(quote: ExecutionQuote) -> bool:
    return (
        quote.is_executable
        and quote.freshness_status == "current"
        and quote.source_kind != "test_fixture"
        and quote.session == "regular"
    )


def _all_visible_quotes_above_max(quote: ExecutionQuote, *, max_price: Decimal) -> bool:
    visible_prices = tuple(price for price in (quote.bid_yen, quote.ask_yen) if price is not None)
    return bool(visible_prices) and all(price > max_price for price in visible_prices)


def _prospective_concentration_warnings(
    policy_input: ExecutionPolicyInput, order_notional: int
) -> tuple[str, ...]:
    portfolio = policy_input.portfolio
    warnings: list[str] = []
    if _pct(portfolio.ticker_exposure_yen + order_notional, portfolio.total_capital_yen) > (
        portfolio.max_ticker_concentration_pct
    ):
        warnings.append("prospective_ticker_concentration_exceeds_warning")
    if _pct(portfolio.sector_exposure_yen + order_notional, portfolio.total_capital_yen) > (
        portfolio.max_sector_concentration_pct
    ):
        warnings.append("prospective_sector_concentration_exceeds_warning")
    for factor in policy_input.common_factors:
        current = portfolio.common_factor_exposure_yen.get(factor, 0)
        if _pct(current + order_notional, portfolio.total_capital_yen) > (
            portfolio.max_common_factor_concentration_pct
        ):
            warnings.append(f"prospective_common_factor_concentration_exceeds_warning:{factor}")
    return tuple(warnings)


def _pct(numerator: int, denominator: int) -> float:
    return float(Decimal(numerator) / Decimal(denominator) * 100)


def _buy_now_option(
    policy_input: ExecutionPolicyInput, *, max_price: Decimal, actionable: bool
) -> ExecutionOption:
    quote = policy_input.quote
    if not actionable:
        return _ineligible_option("buy_now", "quote_not_actionable")
    if quote.ask_yen is None:
        return _ineligible_option("buy_now", "ask_unavailable")
    if quote.ask_yen > max_price:
        return _ineligible_option("buy_now", "ask_above_max_acceptable_price", price=quote.ask_yen)
    return _funded_option("buy_now", quote.ask_yen, policy_input)


def _shallow_option(
    policy_input: ExecutionPolicyInput, *, max_price: Decimal, actionable: bool
) -> ExecutionOption:
    quote = policy_input.quote
    if not actionable:
        return _ineligible_option("shallow_limit", "quote_not_actionable")
    basis = min(quote.bid_yen, max_price) if quote.bid_yen is not None else max_price
    price = _floor_to_tick(basis, quote.tick_size_yen)
    if price <= 0:
        return _ineligible_option("shallow_limit", "no_positive_legal_tick")
    option = _funded_option("shallow_limit", price, policy_input)
    if quote.bid_yen is None:
        return ExecutionOption(
            tactic=option.tactic,
            eligible=option.eligible,
            price_yen=option.price_yen,
            quantity=option.quantity,
            notional_yen=option.notional_yen,
            reasons=(*option.reasons, "max_acceptable_price_fallback_without_bid"),
            warnings=option.warnings,
        )
    return option


def _deep_option(
    policy_input: ExecutionPolicyInput, *, shallow: ExecutionOption, actionable: bool
) -> ExecutionOption:
    if not actionable:
        return _ineligible_option("deep_limit", "quote_not_actionable")
    if policy_input.deep_discount_bps is None:
        return _ineligible_option("deep_limit", "deep_discount_assumption_missing")
    if shallow.price_yen is None:
        return _ineligible_option("deep_limit", "shallow_price_unavailable")
    factor = Decimal(10_000 - policy_input.deep_discount_bps) / 10_000
    price = _floor_to_tick(shallow.price_yen * factor, policy_input.quote.tick_size_yen)
    if price <= 0:
        return _ineligible_option("deep_limit", "no_positive_legal_tick")
    if price >= shallow.price_yen:
        return _ineligible_option("deep_limit", "deep_price_equals_shallow_tick", price=price)
    return _funded_option("deep_limit", price, policy_input)


def _funded_option(
    tactic: Literal["buy_now", "shallow_limit", "deep_limit"],
    price: Decimal,
    policy_input: ExecutionPolicyInput,
) -> ExecutionOption:
    notional = _notional(price, policy_input.quantity)
    cash_after = policy_input.portfolio.available_cash_yen - notional
    if cash_after < policy_input.portfolio.dry_powder_floor_yen:
        return ExecutionOption(
            tactic=tactic,
            eligible=False,
            price_yen=price,
            quantity=policy_input.quantity,
            notional_yen=notional,
            reasons=("dry_powder_cash_boundary_breached",),
            warnings=(),
        )
    return ExecutionOption(
        tactic=tactic,
        eligible=True,
        price_yen=price,
        quantity=policy_input.quantity,
        notional_yen=notional,
        reasons=("within_max_price_and_cash_boundary",),
        warnings=(),
    )


def _ineligible_option(
    tactic: Literal["buy_now", "shallow_limit", "deep_limit"],
    reason: str,
    *,
    price: Decimal | None = None,
) -> ExecutionOption:
    return ExecutionOption(
        tactic=tactic,
        eligible=False,
        price_yen=price,
        quantity=0,
        notional_yen=None,
        reasons=(reason,),
        warnings=(),
    )


def _defer_option() -> ExecutionOption:
    return ExecutionOption(
        tactic="defer",
        eligible=True,
        price_yen=None,
        quantity=0,
        notional_yen=None,
        reasons=("human_can_wait_without_forcing_an_investment",),
        warnings=(),
    )


def _select_tactic(
    policy_input: ExecutionPolicyInput,
    *,
    buy_now: ExecutionOption,
    shallow: ExecutionOption,
    deep: ExecutionOption,
    quote_warnings: Sequence[str],
    force_defer: bool,
) -> Literal["buy_now", "shallow_limit", "deep_limit", "defer"]:
    if force_defer:
        return "defer"
    if policy_input.fill_priority == "fill":
        choices = (buy_now, shallow, deep)
    elif policy_input.fill_priority == "balanced":
        choices = (buy_now, shallow, deep) if not quote_warnings else (shallow, deep, buy_now)
    else:
        choices = (deep, shallow, buy_now)
    return next((option.tactic for option in choices if option.eligible), "defer")


def _proposed_orders(
    policy_input: ExecutionPolicyInput,
    *,
    selected: Literal["buy_now", "shallow_limit", "deep_limit", "defer"],
    shallow: ExecutionOption,
    deep: ExecutionOption,
) -> tuple[ProposedOrder, ...]:
    if selected == "defer":
        return ()
    if (
        selected != "buy_now"
        and policy_input.quantity >= 2 * policy_input.portfolio.board_lot
        and shallow.eligible
        and deep.eligible
        and shallow.price_yen is not None
        and deep.price_yen is not None
    ):
        shallow_quantity = policy_input.portfolio.board_lot
        deep_quantity = policy_input.quantity - shallow_quantity
        ladder_notional = _notional(shallow.price_yen, shallow_quantity) + _notional(
            deep.price_yen, deep_quantity
        )
        if (
            policy_input.portfolio.available_cash_yen - ladder_notional
            >= policy_input.portfolio.dry_powder_floor_yen
        ):
            return (
                ProposedOrder(
                    tactic="shallow_limit",
                    quantity=shallow_quantity,
                    limit_price_yen=shallow.price_yen,
                    expires_at=policy_input.expires_at,
                ),
                ProposedOrder(
                    tactic="deep_limit",
                    quantity=deep_quantity,
                    limit_price_yen=deep.price_yen,
                    expires_at=policy_input.expires_at,
                ),
            )
    selected_option = {"shallow_limit": shallow, "deep_limit": deep}.get(selected)
    if selected == "buy_now":
        price = policy_input.quote.ask_yen
    else:
        price = selected_option.price_yen if selected_option is not None else None
    if price is None:
        raise ExecutionPolicyError("selected tactic has no legal order price")
    return (
        ProposedOrder(
            tactic=selected,
            quantity=policy_input.quantity,
            limit_price_yen=price,
            expires_at=policy_input.expires_at,
        ),
    )


def _floor_to_tick(price: Decimal, tick_size: Decimal) -> Decimal:
    return (price / tick_size).to_integral_value(rounding=ROUND_FLOOR) * tick_size


def _notional(price: Decimal, quantity: int) -> int:
    value = price * quantity
    rounded = value.to_integral_value()
    if value != rounded:
        raise ExecutionPolicyError("order notional must be a whole yen")
    return int(rounded)
