"""提案の一回の収集で台帳・実quote・分類を固定するread owner。"""

from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from datetime import date, datetime
from hashlib import sha256
from pathlib import Path

from baibai_engine.foundation.json import canonical_json
from baibai_engine.foundation.time import JST
from baibai_engine.market.jquants_reader import read_master_snapshot
from baibai_engine.position.ledger import (
    ExecutionEvent,
    MarketPrice,
    replay_events_through,
    summarize_portfolio,
)
from baibai_engine.position.ledger_read import load_ledger_in_transaction
from baibai_engine.position.market_source import market_basis_is_confirmed, read_unadjusted_close
from baibai_engine.position.policy import PORTFOLIO_POLICY
from baibai_engine.position.valuation import holding_episode
from baibai_engine.investment_judgment.stock_assessment import AssessmentRef
from baibai_engine.investment_judgment.stock_assessment_store import (
    ReviewedAssessment,
    latest_assessment_id,
    require_assessment_ref,
)
from baibai_engine.investment_judgment.operation.models import OperationSession
from baibai_engine.investment_judgment.operation.progress import proposal_scope, selection_binding
from baibai_engine.investment_judgment.trade_proposal import (
    LiquidityContext,
    PositionInput,
    ProposalError,
    ProposalInputs,
    ProposalResearchScope,
    QuoteInput,
    RequestScope,
    ReservationInput,
    SecurityClassification,
)
from baibai_engine.investment_judgment.selection import ResearchSelection
from baibai_engine.storage.application.read import connect_read_only
from baibai_engine.foundation.numbers import finite_decimal

DEFAULT_BUDGET_MIN_YEN = 200_000
DEFAULT_BUDGET_MAX_YEN = 300_000


def proposal_input_policy_id() -> str:
    """現在使ったthreshold全体の世代を、入力事実の一部として固定する。"""
    return "portfolio-policy-" + sha256(canonical_json(PORTFOLIO_POLICY).encode()).hexdigest()


def _original_assessment(
    connection: sqlite3.Connection,
    *,
    ticker: str,
    bought: ExecutionEvent | None,
) -> tuple[AssessmentRef | None, str | None]:
    reference = None if bought is None else bought.decision_reference

    def follows_reported_buy(published_at: datetime) -> bool:
        if bought is None:
            return True
        if bought.occurred_on is not None:
            # A date-only report explicitly names its proposal, but supplies no
            # execution clock. Its replay key must not invent an intraday conflict.
            return published_at.astimezone(JST).date() > bought.occurred_on
        return published_at > bought.occurred_at

    if reference is not None:
        row = connection.execute(
            "SELECT ticker,origin,proposal_id FROM position_decision_reference WHERE reference=?",
            (reference,),
        ).fetchone()
        if row is not None and row[0] != ticker:
            raise ProposalError("recorded initial buy binding ticker differs from holding")
        if row is not None and row[1] == "proposal" and row[2] is not None:
            from baibai_engine.investment_judgment.trade_proposal_query import load_investment_proposal

            proposal = load_investment_proposal(connection, str(row[2]))
            item = next(
                (item for item in proposal.items if item.ticker == ticker and item.action == "buy"),
                None,
            )
            if item is None:
                raise ProposalError("initial buy reference has no matching saved buy item")
            published = connection.execute(
                "SELECT published_at FROM stock_assessment WHERE assessment_id=?",
                (item.assessment_ref.assessment_id,),
            ).fetchone()
            if follows_reported_buy(datetime.fromisoformat(str(published[0]))):
                return (
                    None,
                    (
                        f"判断参照 {reference} の評価公開は買付より後です。"
                        "買付時の評価と確認できず、現在評価を当初理由として補完しない。"
                    ),
                )
            if follows_reported_buy(proposal.published_at):
                return (
                    None,
                    (
                        f"判断参照 {reference} の提案公開は買付より後です。"
                        "買付時の提案と確認できず、後から作った提案を当初理由として補完しない。"
                    ),
                )
            return item.assessment_ref, None
    label = "報告に判断参照なし" if reference is None else f"判断参照 {reference}"
    return None, f"{label}の当初評価・投資理由を確認できない。現在評価では補完しない。"


def collect_proposal_inputs(
    connection: sqlite3.Connection,
    *,
    tickers: tuple[str, ...],
    proposal_id: str,
    now: datetime,
    sqlite_path: Path,
    budget_min_yen: int,
    budget_max_yen: int,
) -> ProposalInputs:
    """callerのread/write transactionで台帳headとmetaを同じ世代から読む。"""
    if not connection.in_transaction:
        raise ProposalError("proposal input collection requires an existing transaction")
    if now.utcoffset() is None:
        raise ProposalError("proposal clock requires a timezone")
    ledger, head = load_ledger_in_transaction(connection)
    if ledger.as_of > now or any(event.occurred_at > now for event in ledger.events):
        raise ProposalError("ledger contains facts after the proposal instant")
    state = replay_events_through(ledger.events, now)
    held_tickers = {ticker for ticker, lots in state.lots.items() if lots}
    quotes: list[QuoteInput] = []
    quantity_basis: dict[str, bool] = {}
    prices: dict[str, MarketPrice] = {}
    for ticker in sorted(set(tickers) | held_tickers):
        observed = read_unadjusted_close(sqlite_path=sqlite_path, ticker=ticker, at=now)
        if observed is None:
            quantity_basis[ticker] = False
            continue
        quote = QuoteInput(
            ticker=ticker,
            price_yen=finite_decimal(observed.close_yen),
            price_as_of=observed.price_as_of,
            price_basis="last_close_unadjusted",
            source_ref="jquants_daily_bars",
        )
        quotes.append(quote)
        if ticker in held_tickers:
            first_buy, last_trade = holding_episode(ledger, ticker, now)
            confirmed = (
                first_buy is not None
                and last_trade is not None
                and market_basis_is_confirmed(
                    basis="shares",
                    sqlite_path=sqlite_path,
                    ticker=ticker,
                    from_date=min(first_buy.occurred_at.astimezone(JST).date(), quote.price_as_of),
                    through_date=max(last_trade.astimezone(JST).date(), quote.price_as_of),
                )
            )
            quantity_basis[ticker] = confirmed
            if (
                confirmed
                and 0
                <= (now.astimezone(JST).date() - quote.price_as_of).days
                <= (PORTFOLIO_POLICY["valuation"]["market_price_max_age_days"])
            ):
                prices[ticker] = MarketPrice(
                    ticker=ticker,
                    price_yen=quote.price_yen,
                    price_as_of=quote.price_as_of,
                    source_kind="market_api",
                    price_basis="unadjusted_close",
                    source_ref=quote.source_ref,
                )
    portfolio = summarize_portfolio(ledger, state, prices)
    positions: list[PositionInput] = []
    for holding in portfolio.holdings:
        bought, _ = holding_episode(ledger, holding.ticker, now)
        original_ref, unavailable = _original_assessment(
            connection, ticker=holding.ticker, bought=bought
        )
        positions.append(
            PositionInput(
                ticker=holding.ticker,
                sector=holding.sector,
                common_factors=holding.common_factors,
                quantity=holding.quantity,
                cost_yen=finite_decimal(holding.deployed_cost_yen),
                quantity_basis_confirmed=quantity_basis.get(holding.ticker, False),
                original_assessment_ref=original_ref,
                original_reason_unavailable_reason=unavailable,
            )
        )
    reservations = tuple(
        ReservationInput(
            reservation_id=item.reservation_id,
            ticker=item.ticker,
            reserved_yen=finite_decimal(item.reserved_yen),
            decision_reference=item.decision_reference,
            sector=item.sector,
            common_factors=item.common_factors,
        )
        for item in portfolio.active_reservations
    )
    master = read_master_snapshot(sqlite_path, as_of=now.astimezone(JST).date())
    classifications: list[SecurityClassification] = []
    if master is not None:
        for ticker in sorted(set(tickers) | held_tickers):
            row = master.securities.get(ticker)
            if row is not None and row["market_segment"]:
                classifications.append(
                    SecurityClassification(
                        ticker=ticker,
                        market_segment=str(row["market_segment"]),
                        is_common_stock=bool(row["is_common_stock"]),
                        snapshot_date=master.as_of,
                    )
                )
    return ProposalInputs(
        checked_at=now,
        ledger_head=head,
        ledger_as_of=ledger.as_of,
        available_cash_yen=finite_decimal(state.available_cash_yen),
        positions=tuple(sorted(positions, key=lambda position: position.ticker)),
        active_reservations=reservations,
        prior_buy_execution_references=tuple(
            sorted(
                event.execution_id
                for event in ledger.events
                if isinstance(event, ExecutionEvent)
                and event.side == "buy"
                and event.decision_reference == proposal_id
            )
        ),
        quotes=tuple(quotes),
        policy_id=proposal_input_policy_id(),
        budget_min_yen=budget_min_yen,
        budget_max_yen=budget_max_yen,
        board_lot=PORTFOLIO_POLICY["order_constraints"]["board_lot"],
        security_classification=tuple(classifications),
        total_capital_yen=None
        if portfolio.total_capital_yen is None
        else finite_decimal(portfolio.total_capital_yen),
    )


def proposal_liquidity_context(selection: ResearchSelection) -> tuple[LiquidityContext, ...]:
    snapshot = selection.inputs.analysis
    result: list[LiquidityContext] = []
    for target in selection.targets:
        row = snapshot.analyses.get(target.ticker, {})
        try:
            adv = finite_decimal(row.get("avg_turnover_oku")) * 100_000_000
        except ValueError:
            adv = None
        result.append(
            LiquidityContext(
                ticker=target.ticker,
                run_revision_id=snapshot.run_revision_id,
                analysis_as_of=snapshot.analysis_as_of,
                rules_hash=snapshot.screening_rules_hash,
                adv_yen=adv,
                unavailable_reason=None
                if adv is not None
                else snapshot.unavailable.get(target.ticker, "保存選択snapshotにADV値が無い"),
            )
        )
    return tuple(result)


@dataclass(frozen=True, slots=True)
class ProposalAuthoringContext:
    operation_id: str
    proposal_id: str | None
    request_scope: RequestScope
    research_scope: ProposalResearchScope
    inputs: ProposalInputs
    assessments: dict[str, ReviewedAssessment]
    liquidity_context: tuple[LiquidityContext, ...]
    market_context_assessment_id: str | None
    latest_assessment_ids: dict[str, str | None]
    next_order_session: date | None
    calculations: dict[str, dict[str, object]]

    def payload(self) -> dict[str, object]:
        return {
            "operation_id": self.operation_id,
            "proposal_id": self.proposal_id,
            "request_scope": self.request_scope,
            "research_scope": self.research_scope.model_dump(mode="json"),
            "inputs": self.inputs.model_dump(mode="json"),
            "assessments": {
                identity: {
                    "assessment_id": pair.assessment_id,
                    "core_sha256": pair.core_sha256,
                    "document": pair.document.model_dump(mode="json"),
                    "review": pair.review.model_dump(mode="json"),
                }
                for identity, pair in self.assessments.items()
            },
            "latest_assessment_ids": self.latest_assessment_ids,
            "liquidity_context": [item.model_dump(mode="json") for item in self.liquidity_context],
            "market_context_assessment_id": self.market_context_assessment_id,
            "next_order_session": None
            if self.next_order_session is None
            else self.next_order_session.isoformat(),
            "calculations": self.calculations,
        }


def _context_calculations(
    scope: ProposalResearchScope,
    inputs: ProposalInputs,
    assessments: dict[str, ReviewedAssessment],
    latest: dict[str, str | None],
    *,
    sqlite_path: Path,
    now: datetime,
    session: date | None,
) -> dict[str, dict[str, object]]:
    from baibai_engine.position.price_grid import floor_common_stock_limit
    from baibai_engine.investment_judgment.stock_assessment import current_price_projection
    from baibai_engine.investment_judgment.trade_proposal_conditions import (
        assessment_quote_basis,
        buy_evaluation,
        buy_evaluation_payload,
    )
    from baibai_engine.investment_judgment.scenario import maximum_entry_price

    result: dict[str, dict[str, object]] = {}
    for outcome in scope.outcomes:
        if outcome.status != "reviewed":
            continue
        pair = assessments[outcome.assessment_ref.assessment_id]
        quote = next((quote for quote in inputs.quotes if quote.ticker == outcome.ticker), None)
        basis = assessment_quote_basis(pair.document, quote, sqlite_path=sqlite_path)
        row: dict[str, object] = {
            "current_quote_projection": current_price_projection(
                pair.document,
                price_yen=None if quote is None else quote.price_yen,
                price_as_of=None if quote is None else quote.price_as_of,
                as_of=now.astimezone(JST).date(),
                basis_confirmed=basis,
                max_quote_age_days=PORTFOLIO_POLICY["valuation"]["market_price_max_age_days"],
            ),
            "case_reassessment_required": outcome.purpose == "holding",
        }
        if outcome.purpose == "new" and quote is not None:
            row["buy_at_actual_quote"] = buy_evaluation_payload(
                buy_evaluation(
                    pair,
                    inputs=inputs,
                    proposed_limit_yen=quote.price_yen,
                    latest=latest[outcome.ticker] == pair.assessment_id,
                    sqlite_path=sqlite_path,
                    now=now,
                )
            )
            valuation = pair.document.valuation
            maximum = (
                None
                if valuation.base is None
                or valuation.horizon_months is None
                or valuation.required_annual_return_pct is None
                else maximum_entry_price(
                    valuation.base,
                    horizon_months=valuation.horizon_months,
                    required_annual_return_pct=valuation.required_annual_return_pct,
                )
            )
            if maximum is not None and maximum > 0:
                row["calculation_at_maximum_price"] = buy_evaluation_payload(
                    buy_evaluation(
                        pair,
                        inputs=inputs,
                        proposed_limit_yen=maximum,
                        latest=latest[outcome.ticker] == pair.assessment_id,
                        sqlite_path=sqlite_path,
                        now=now,
                    )
                )
                classification = next(
                    (row for row in inputs.security_classification if row.ticker == outcome.ticker),
                    None,
                )
                grid = (
                    None
                    if classification is None or session is None
                    else floor_common_stock_limit(
                        maximum,
                        target_session=session,
                        market_segment=classification.market_segment,
                        is_common_stock=classification.is_common_stock,
                    )
                )
                row["maximum_common_grid_price_yen"] = None if grid is None else str(grid)
        result[outcome.ticker] = row
    return result


def build_proposal_context(
    *,
    operation_id: str,
    db_path: Path | None,
    sqlite_path: Path,
    now: datetime,
    proposal_id: str | None = None,
) -> ProposalAuthoringContext:
    from baibai_engine.position.market_source import next_order_session

    with closing(connect_read_only(db_path)) as connection:
        connection.execute("BEGIN")
        row = connection.execute(
            "SELECT * FROM operation_session WHERE operation_id=?", (operation_id,)
        ).fetchone()
        if row is None:
            raise ProposalError(f"Research Operation is unavailable: {operation_id}")
        operation = OperationSession.model_validate(
            {**dict(row), "payload": json.loads(str(row["payload"]))}
        )
        selection = selection_binding(operation.payload)
        if operation.status != "active" or selection is None:
            raise ProposalError("proposal authoring requires an active selected Research Operation")
        if operation.started_at > now:
            raise ProposalError("Research Operation starts after the authoring instant")
        scope = proposal_scope(operation.operation_id, operation.payload)
        inputs = collect_proposal_inputs(
            connection,
            tickers=tuple(target.ticker for target in scope.targets),
            proposal_id=proposal_id or "",
            now=now,
            sqlite_path=sqlite_path,
            budget_min_yen=DEFAULT_BUDGET_MIN_YEN,
            budget_max_yen=DEFAULT_BUDGET_MAX_YEN,
        )
        assessments = {
            outcome.assessment_ref.assessment_id: require_assessment_ref(
                connection, outcome.assessment_ref
            )
            for outcome in scope.outcomes
            if outcome.status == "reviewed"
        }
        latest = {
            target.ticker: latest_assessment_id(connection, target.ticker)
            for target in scope.targets
        }
        saved_context = selection.inputs.market_context.get("saved_market_context_assessment")
        context_id = saved_context.get("context_id") if isinstance(saved_context, dict) else None
        if context_id is not None and not isinstance(context_id, str):
            raise ProposalError("saved Market Context Assessment identity must be text")
        session = next_order_session(sqlite_path=sqlite_path, now=now)
        return ProposalAuthoringContext(
            operation_id,
            proposal_id,
            selection.request.scope,
            scope,
            inputs,
            assessments,
            proposal_liquidity_context(selection),
            context_id,
            latest,
            session,
            _context_calculations(
                scope,
                inputs,
                assessments,
                latest,
                sqlite_path=sqlite_path,
                now=now,
                session=session,
            ),
        )
