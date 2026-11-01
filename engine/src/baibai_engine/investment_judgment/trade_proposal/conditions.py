"""現在の提案条件を読み取り専用で評価する。公開transactionやstore書込は所有しない。"""

from __future__ import annotations

import sqlite3
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING

from baibai_engine.investment_judgment.stock_assessment.document import StockAssessmentDocument
from baibai_engine.investment_judgment.trade_proposal.contracts import (
    BuyItem,
    GateReason,
    ProposalInputs,
    QuoteInput,
    SellItem,
)
from baibai_engine.investment_judgment.trade_proposal.entry_policy import (
    BuyTermsEvaluation,
    evaluate_buy_terms,
)
from baibai_engine.machine_analysis.observations.stocks.universe import ELIGIBLE_MARKETS
from baibai_engine.portfolio.holdings.market_observations import (
    market_basis_is_confirmed,
    next_order_session,
)
from baibai_engine.portfolio.holdings.price_grid import (
    PRICE_GRID_CONFIRMED_AT,
    PRICE_GRID_VALID_UNTIL,
)
from baibai_engine.portfolio.policy import PORTFOLIO_POLICY
from baibai_engine.primitives.time import JST

if TYPE_CHECKING:
    from baibai_engine.investment_judgment.stock_assessment.store import ReviewedAssessment


def proposal_quote(inputs: ProposalInputs, ticker: str) -> QuoteInput | None:
    return next((quote for quote in inputs.quotes if quote.ticker == ticker), None)


def assessment_quote_basis(
    assessment: StockAssessmentDocument,
    quote: QuoteInput | None,
    *,
    sqlite_path: Path,
    market_connection: sqlite3.Connection | None,
) -> bool:
    price_fact = next(
        (
            fact
            for fact in assessment.input_snapshot.facts
            if fact.fact_id == assessment.valuation.market_price_fact_id
        ),
        None,
    )
    return (
        quote is not None
        and market_connection is not None
        and price_fact is not None
        and market_basis_is_confirmed(
            basis="price",
            sqlite_path=sqlite_path,
            ticker=quote.ticker,
            from_date=price_fact.as_of,
            through_date=quote.price_as_of,
            connection=market_connection,
        )
    )


def buy_evaluation(
    pair: ReviewedAssessment,
    *,
    inputs: ProposalInputs,
    proposed_limit_yen: Decimal,
    latest: bool,
    sqlite_path: Path,
    now: datetime,
    market_connection: sqlite3.Connection | None,
    selected_quantity: int | None = None,
) -> BuyTermsEvaluation:
    quote = proposal_quote(inputs, pair.document.input_snapshot.ticker)
    ticker = pair.document.input_snapshot.ticker
    return evaluate_buy_terms(
        pair.document,
        reviewed=True,
        latest=latest,
        as_of=now.astimezone(JST).date(),
        quote=quote,
        proposed_limit_yen=proposed_limit_yen,
        selected_quantity=selected_quantity,
        basis_confirmed=assessment_quote_basis(
            pair.document, quote, sqlite_path=sqlite_path, market_connection=market_connection
        ),
        market_price_max_age_days=PORTFOLIO_POLICY["valuation"]["market_price_max_age_days"],
        held_quantity=sum(
            position.quantity for position in inputs.positions if position.ticker == ticker
        ),
        active_reservation=any(
            reservation.ticker == ticker for reservation in inputs.active_reservations
        ),
        proposal_executed=bool(inputs.prior_buy_execution_references),
        available_cash_yen=inputs.available_cash_yen,
        budget_max_yen=Decimal(inputs.budget_max_yen),
        board_lot=inputs.board_lot,
    )


def buy_evaluation_payload(result: BuyTermsEvaluation) -> dict[str, object]:
    return {
        "hard_blockers": [reason.model_dump(mode="json") for reason in result.hard_blockers],
        "maximum_price_yen": None
        if result.maximum_price_yen is None
        else str(result.maximum_price_yen),
        "quantity": result.quantity,
        "maximum_quantity": result.maximum_quantity,
        "current_price_above_maximum": result.current_price_above_maximum,
        "current_price_projection": result.current_price_projection,
        "limit_price_projection": result.limit_price_projection,
    }


def terms_unavailable_reasons(
    item: BuyItem | SellItem,
    inputs: ProposalInputs,
    *,
    sqlite_path: Path,
    now: datetime,
    market_connection: sqlite3.Connection | None = None,
) -> tuple[GateReason, ...]:
    """経済判断と独立した、具体注文条件の不足を現在入力から列挙する。"""
    reasons: list[GateReason] = []
    quote = proposal_quote(inputs, item.ticker)
    if (
        quote is None
        or not 0
        <= (now.astimezone(JST).date() - quote.price_as_of).days
        <= (PORTFOLIO_POLICY["valuation"]["market_price_max_age_days"])
    ):
        reasons.append(
            GateReason(
                code="quote_missing_or_stale", message="現在quoteが欠測または鮮度期限外です。"
            )
        )
    if item.action == "sell":
        if any(reservation.ticker == item.ticker for reservation in inputs.active_reservations):
            reasons.append(
                GateReason(
                    code="active_reservation_exists",
                    message="同銘柄の既存注文の処理確認が必要です。",
                )
            )
        position = next((row for row in inputs.positions if row.ticker == item.ticker), None)
        if (
            position is None
            or not position.quantity_basis_confirmed
            or position.quantity % inputs.board_lot
        ):
            reasons.append(
                GateReason(
                    code="quantity_basis_unconfirmed",
                    message="全保有数量・株式単位または端株ルートの確認が必要です。",
                )
            )
    classification = next(
        (row for row in inputs.security_classification if row.ticker == item.ticker), None
    )
    if (
        classification is None
        or classification.market_segment.strip().upper() not in ELIGIBLE_MARKETS
        or not classification.is_common_stock
    ):
        reasons.append(
            GateReason(
                code="security_classification_unavailable",
                message="原本の商品区分・銘柄コード・市場から東証普通株と確認できません。",
            )
        )
    session = next_order_session(sqlite_path=sqlite_path, now=now, connection=market_connection)
    if session is None:
        reasons.append(
            GateReason(
                code="order_session_unknown", message="次の注文可能sessionを確認できません。"
            )
        )
    elif not PRICE_GRID_CONFIRMED_AT <= session < PRICE_GRID_VALID_UNTIL:
        reasons.append(
            GateReason(
                code="price_grid_rule_unavailable",
                message="次sessionへ適用できる呼値ruleが未確定です。",
            )
        )
    return tuple(reasons)
