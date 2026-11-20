"""現在の提案条件を読み取り専用で評価する。公開transactionやstore書込は所有しない。"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING

from baibai_engine.investment_judgment.stock_assessment.document import StockAssessmentDocument
from baibai_engine.investment_judgment.stock_assessment.price_value import referenced_price_fact
from baibai_engine.investment_judgment.trade_proposal.contracts import (
    BuyItem,
    GateReason,
    ProposalInputs,
    QuoteInput,
    SellItem,
)
from baibai_engine.investment_judgment.trade_proposal.entry_policy import (
    BuyTermsEvaluation,
    acquisition_blocker,
    prepare_buy_terms,
)
from baibai_engine.machine_analysis.observations.prices.sessions import latest_eligible_close_date
from baibai_engine.machine_analysis.observations.stocks.universe import ELIGIBLE_MARKETS
from baibai_engine.portfolio.holdings.market_observations import market_basis_is_confirmed
from baibai_engine.portfolio.holdings.price_grid import (
    PRICE_GRID_CONFIRMED_AT,
    PRICE_GRID_VALID_UNTIL,
)
from baibai_engine.portfolio.policy import PORTFOLIO_POLICY
from baibai_engine.primitives.numbers import finite_decimal
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
    now: datetime,
    market_connection: sqlite3.Connection | None,
) -> bool:
    """Compare quote units, including received actions through the judgment day."""
    price_fact = referenced_price_fact(assessment)
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
            known_actions_through=now.astimezone(JST).date(),
            connection=market_connection,
        )
    )


def no_action_blocker(
    pair: ReviewedAssessment,
    *,
    inputs: ProposalInputs,
    sqlite_path: Path,
    now: datetime,
    market_connection: sqlite3.Connection | None,
) -> GateReason | None:
    """新規見送りの根拠を確認。資本代替の内容は提案の独立Reviewに委ねる。"""
    assessment = pair.document
    thesis = assessment.investment_case
    if (
        thesis.status == "broken"
        and pair.review.primary_source_check == "verified"
        and any(
            source.source_tier == "primary" and source.source_id in thesis.source_ids
            for source in assessment.input_snapshot.sources
        )
    ):
        return None  # 確認済みの事業反証は、株価や数値評価を必要としない。
    finding = assessment.price_value_case
    if (
        finding is not None
        and finding.status == "supported_at_quote"
        and assessment.conclusion.stance != "uncertain"
    ):
        return None  # 価格の支持は同じ資本の用途まで決めない。
    if finding is not None and (
        finding.status == "not_supported_at_quote"
        or (
            finding.status == "supported_at_quote"
            and pair.review.primary_source_check == "verified"
        )
    ):
        quote = proposal_quote(inputs, assessment.input_snapshot.ticker)
        observed = referenced_price_fact(assessment)
        day = now.astimezone(JST).date()
        # 実際の次立会が不明でも、当日引け以降にその立会で取得できないことは分かる。
        earliest_possible_trade = latest_eligible_close_date(now) + timedelta(days=1)
        if (
            quote is not None
            and observed is not None
            and not isinstance(observed.value, bool | str)
            and assessment.input_snapshot.as_of == day
            and observed.as_of == quote.price_as_of
            and observed.price_basis == quote.price_basis
            and finite_decimal(observed.value) == quote.price_yen
            and (observed.observed_at is None or observed.observed_at <= inputs.checked_at)
            and acquisition_blocker(assessment, earliest_possible_trade) is None
            and assessment_quote_basis(
                assessment,
                quote,
                sqlite_path=sqlite_path,
                now=now,
                market_connection=market_connection,
            )
        ):
            return None
        return GateReason(
            code="no_action_reviewed_quote_mismatch",
            message="査読した終値・日付・株式単位・取得権を今回の比較と照合できません。再評価するかdeferを使ってください。",
        )
    return GateReason(
        code="no_action_comparison_incomplete",
        message="確認済みの事業反証も今回の経済比較も未確定です。deferと再確認条件を使ってください。",
    )


def buy_evaluator(
    pair: ReviewedAssessment,
    *,
    inputs: ProposalInputs,
    target_session: date | None,
    latest: bool,
    sqlite_path: Path,
    now: datetime,
    market_connection: sqlite3.Connection | None,
) -> Callable[[Decimal, int | None], BuyTermsEvaluation]:
    quote = proposal_quote(inputs, pair.document.input_snapshot.ticker)
    ticker = pair.document.input_snapshot.ticker
    return prepare_buy_terms(
        pair.document,
        reviewed=True,
        latest=latest,
        as_of=now.astimezone(JST).date(),
        quote=quote,
        target_session=target_session,
        basis_confirmed=assessment_quote_basis(
            pair.document,
            quote,
            sqlite_path=sqlite_path,
            now=now,
            market_connection=market_connection,
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


def buy_evaluation_payload(
    result: BuyTermsEvaluation, *, order_blockers: tuple[GateReason, ...] = ()
) -> dict[str, object]:
    # 終値・仮の指値での一株価値は、元の注文案を再利用できることとは別。
    reasons = {reason.code: reason for reason in (*order_blockers, *result.hard_blockers)}
    return {
        "hard_blockers": [reason.model_dump(mode="json") for reason in reasons.values()],
        "maximum_price_yen": None
        if result.maximum_price_yen is None
        else str(result.maximum_price_yen),
        "quantity": 0 if order_blockers else result.quantity,
        "maximum_quantity": None if order_blockers else result.maximum_quantity,
        "current_price_above_maximum": result.current_price_above_maximum,
        "current_price_projection": result.current_price_projection,
        "limit_price_projection": result.limit_price_projection,
        "capital_comparison": None if order_blockers else result.capital_comparison,
    }


def terms_unavailable_reasons(
    item: BuyItem | SellItem,
    inputs: ProposalInputs,
    *,
    now: datetime,
    session: date | None,
) -> tuple[GateReason, ...]:
    """経済判断と独立した、具体注文条件の不足を現在入力から列挙する。"""
    if item.action == "sell" and item.conditional_recovery is not None:
        return (
            GateReason(
                code="conditional_recovery_not_order_terms",
                message="台帳上の保有を減らす条件案です。実口座・売却可能数・未成立注文・売値は未確認で、注文条件は提示しません。",
            ),
        )
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
    if session is not None and item.terms is not None and item.terms.target_session != session:
        reasons.append(
            GateReason(
                code="target_session_not_next_available",
                message="保存条件の対象日が次の注文可能sessionと異なります。",
            )
        )
    return tuple(reasons)
