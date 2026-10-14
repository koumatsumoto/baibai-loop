"""固定した提案入力だけから、予算・流動性・資本集中の最低限診断を産む。"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal, localcontext
from typing import Literal

from baibai_engine.foundation.time import JST
from baibai_engine.position.policy import PORTFOLIO_POLICY
from baibai_engine.research.assessment import StockAssessmentDocument
from baibai_engine.research.proposal import (
    ExposureRow,
    LiquidityContext,
    PortfolioExposure,
    ProposalError,
    ProposalInputs,
    ProposalItem,
)
from baibai_engine.research.proposal_policy import MAX_NEW_BUYS
from baibai_engine.valuation.scenario import finite_decimal


@dataclass(frozen=True, slots=True)
class ProposalWarnings:
    terms_warnings: dict[str, tuple[str, ...]]
    warnings: tuple[str, ...]
    portfolio_exposure: PortfolioExposure


def build_proposal_warnings(
    inputs: ProposalInputs,
    items: tuple[ProposalItem, ...],
    liquidity_context: tuple[LiquidityContext, ...],
    *,
    assessments: Mapping[str, StockAssessmentDocument],
) -> ProposalWarnings:
    """Use caller-verified exact assessment pairs; never read a current run or add sell proceeds."""
    buys = [item for item in items if item.action == "buy"]
    if len(buys) > MAX_NEW_BUYS:
        raise ProposalError(f"current proposal policy permits at most {MAX_NEW_BUYS} new buy")
    ready_buys = [item for item in buys if item.terms is not None]
    quotes = {quote.ticker: quote for quote in inputs.quotes}
    checked_date = inputs.checked_at.astimezone(JST).date()
    values: dict[str, Decimal] = {}
    warnings: list[str] = []
    for position in inputs.positions:
        quote = quotes.get(position.ticker)
        if (
            quote is None
            or not quote.basis_confirmed
            or not position.quantity_basis_confirmed
            or not 0
            <= (checked_date - quote.price_as_of).days
            <= PORTFOLIO_POLICY["valuation"]["market_price_max_age_days"]
        ):
            warnings.append(f"portfolio_valuation_unknown:{position.ticker}")
        else:
            values[position.ticker] = quote.price_yen * position.quantity
    unclassified = {
        position.ticker for position in inputs.positions if not position.common_factors
    } | {
        reservation.ticker
        for reservation in inputs.active_reservations
        if not reservation.common_factors
    }
    for buy in ready_buys:
        document = assessments.get(buy.assessment_ref.assessment_id)
        if document is None or document.input_snapshot.ticker != buy.ticker:
            raise ProposalError("buy warning metadata requires its exact assessment document")
        if not document.input_snapshot.common_factors:
            unclassified.add(buy.ticker)
    if unclassified:
        warnings.append("portfolio_exposure_common_factor_coverage_incomplete")
    nav = inputs.total_capital_yen
    assessable = nav is not None and nav > 0 and len(values) == len(inputs.positions)
    if not assessable:
        warnings.append("concentration_and_dry_powder_unassessed")
    exposure = PortfolioExposure(
        total_capital_yen=nav,
        ticker=None,
        sector=None,
        common_factors=(),
        common_factor_unclassified_tickers=tuple(sorted(unclassified)),
    )
    liquidity = {context.ticker: context for context in liquidity_context}
    metadata = {
        position.ticker: (position.sector, position.common_factors) for position in inputs.positions
    }

    def value_for(scope: Literal["ticker", "sector", "common_factor"], key: str) -> Decimal:
        def matches(ticker: str, sector: str, factors: tuple[str, ...]) -> bool:
            return (
                (scope == "ticker" and ticker == key)
                or (scope == "sector" and sector == key)
                or (scope == "common_factor" and key in factors)
            )

        held_value = sum(
            (value for ticker, value in values.items() if matches(ticker, *metadata[ticker])),
            Decimal(0),
        )
        return held_value + sum(
            (
                reservation.reserved_yen
                for reservation in inputs.active_reservations
                if matches(reservation.ticker, reservation.sector, reservation.common_factors)
            ),
            Decimal(0),
        )

    terms_warnings: dict[str, tuple[str, ...]] = {}
    for buy in ready_buys:
        assert buy.terms is not None
        notional = buy.terms.notional_yen
        buy_warnings: list[str] = []
        if notional < inputs.budget_min_yen:
            buy_warnings.append("budget_guide_under")
        if notional > inputs.budget_max_yen:
            buy_warnings.append("budget_guide_exceeded")
        context = liquidity.get(buy.ticker)
        adv = None if context is None else context.adv_yen
        with localcontext() as arithmetic:
            arithmetic.prec = 50
            if adv is None or adv <= 0:
                buy_warnings.append("adv_participation_unassessed")
            elif notional * 100 / adv > finite_decimal(
                PORTFOLIO_POLICY["risk_budget"]["max_adv_participation_pct"]
            ):
                buy_warnings.append("adv_participation_exceeds_warning")
            if assessable:
                assert nav is not None
                snapshot = assessments[buy.assessment_ref.assessment_id].input_snapshot

                def row_for(
                    scope: Literal["ticker", "sector", "common_factor"],
                    key: str,
                    threshold: Decimal,
                    *,
                    notional: Decimal = notional,
                    buy_warnings: list[str] = buy_warnings,
                ) -> ExposureRow:
                    percentage = (value_for(scope, key) + notional) * 100 / nav
                    if percentage > threshold:
                        buy_warnings.append(
                            f"prospective_{scope}_concentration_exceeds_warning:{key}"
                        )
                    return ExposureRow(key=key, prospective_pct=percentage, warning_pct=threshold)

                exposure = exposure.model_copy(
                    update={
                        "ticker": row_for(
                            "ticker",
                            buy.ticker,
                            finite_decimal(
                                PORTFOLIO_POLICY["risk_budget"]["max_ticker_concentration_pct"]
                            ),
                        ),
                        "sector": row_for(
                            "sector",
                            snapshot.sector,
                            finite_decimal(
                                PORTFOLIO_POLICY["risk_budget"]["max_sector_concentration_pct"]
                            ),
                        ),
                        "common_factors": tuple(
                            row_for(
                                "common_factor",
                                factor,
                                finite_decimal(
                                    PORTFOLIO_POLICY["risk_budget"][
                                        "max_common_factor_concentration_pct"
                                    ]
                                ),
                            )
                            for factor in snapshot.common_factors
                        ),
                    }
                )
                if (
                    inputs.available_cash_yen - notional
                    < nav
                    * finite_decimal(PORTFOLIO_POLICY["cash_management"]["dry_powder_warning_pct"])
                    / 100
                ):
                    buy_warnings.append("dry_powder_below_floor")
        terms_warnings[buy.ticker] = tuple(buy_warnings)
    return ProposalWarnings(terms_warnings, tuple(sorted(warnings)), exposure)
