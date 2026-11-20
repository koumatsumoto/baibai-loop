"""提案の一回の収集で台帳・実quote・分類を固定するread owner。"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from contextlib import closing, contextmanager
from copy import deepcopy
from dataclasses import dataclass
from datetime import date, datetime
from hashlib import sha256
from pathlib import Path

from pydantic import JsonValue

from baibai_engine.investment_judgment.stock_assessment.document import AssessmentRef
from baibai_engine.investment_judgment.stock_assessment.store import (
    ReviewedAssessment,
    latest_assessment_id,
    require_assessment_ref,
)
from baibai_engine.investment_judgment.trade_proposal.contracts import (
    GeneratedLiquidityContext,
    LiquidityContext,
    PositionInput,
    ProposalError,
    ProposalInputs,
    ProposalResearchScope,
    QuoteInput,
    RequestScope,
    ReservationInput,
    SecurityClassification,
    TradeProposal,
)
from baibai_engine.investment_judgment.trade_proposal.query import (
    load_investment_proposal,
    previous_buy_proposals,
    proposal_records,
)
from baibai_engine.investment_judgment.workflow.generated_inputs import project_generated_inputs
from baibai_engine.investment_judgment.workflow.operation.models import OperationSession
from baibai_engine.investment_judgment.workflow.operation.progress import (
    proposal_scope,
    selection_binding,
)
from baibai_engine.investment_judgment.workflow.profit_context import saved_profit_context
from baibai_engine.investment_judgment.workflow.screening_brief import saved_discovery_context
from baibai_engine.investment_judgment.workflow.selection import (
    GeneratedSelection,
    saved_market_assessment,
)
from baibai_engine.investment_judgment.workflow.selection import (
    ResearchSelectionRecord as ResearchSelection,
)
from baibai_engine.machine_analysis.observations.disclosures.listing_designations import (
    ListingDesignationCheck,
    read_listing_designations,
)
from baibai_engine.machine_analysis.observations.disclosures.tender_filing_lookup import (
    TenderFilingLookup,
    read_tender_filing_references,
)
from baibai_engine.machine_analysis.observations.stocks.reader import (
    read_master_snapshot,
)
from baibai_engine.portfolio.holdings.market_observations import read_unadjusted_close
from baibai_engine.portfolio.holdings.valuation import (
    UnresolvedHoldingQuantityError,
    holding_episode,
    holding_market_price,
    holding_quote,
    require_closed_holding_basis,
)
from baibai_engine.portfolio.ledger.events import (
    ExecutionEvent,
    MarketPrice,
    replay_events_through,
    summarize_portfolio,
)
from baibai_engine.portfolio.ledger.read import load_ledger_in_transaction
from baibai_engine.portfolio.policy import PORTFOLIO_POLICY
from baibai_engine.primitives.json import canonical_json
from baibai_engine.primitives.numbers import finite_decimal
from baibai_engine.primitives.time import JST
from baibai_engine.storage.sqlite.application.read import connect_read_only
from baibai_engine.storage.sqlite.source_cache.read import (
    connect_read_only as connect_market_read_only,
)

DEFAULT_BUDGET_MIN_YEN = 200_000
DEFAULT_BUDGET_MAX_YEN = 300_000
PROPOSAL_TERMS_POLICY_REVISION = "reviewed-price-case-and-author-terms-v2"

type _ReportedBuy = tuple[str, TradeProposal]


def proposal_input_policy_id() -> str:
    """提案条件の意味revisionと使用thresholdを新規入力へ固定する。"""
    policy = {"proposal_terms": PROPOSAL_TERMS_POLICY_REVISION, "portfolio": PORTFOLIO_POLICY}
    return "proposal-policy-" + sha256(canonical_json(policy).encode()).hexdigest()


def _original_assessment(
    connection: sqlite3.Connection,
    *,
    ticker: str,
    bought: ExecutionEvent | None,
) -> tuple[AssessmentRef | None, str | None, _ReportedBuy | None]:
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
                    (reference, proposal),
                )
            if follows_reported_buy(proposal.published_at):
                return (
                    None,
                    (
                        f"判断参照 {reference} の提案公開は買付より後です。"
                        "買付時の提案と確認できず、後から作った提案を当初理由として補完しない。"
                    ),
                    (reference, proposal),
                )
            return item.assessment_ref, None, (reference, proposal)
    label = "報告に判断参照なし" if reference is None else f"判断参照 {reference}"
    return None, f"{label}の当初評価・投資理由を確認できない。現在評価では補完しない。", None


@contextmanager
def proposal_market_snapshot(
    sqlite_path: Path, *, connection: sqlite3.Connection | None = None
) -> Iterator[sqlite3.Connection | None]:
    """Keep quotes, classification, calendar and share/assessment basis on one snapshot."""
    if connection is not None:
        yield connection
    elif sqlite_path.exists():
        with closing(connect_market_read_only(sqlite_path)) as market_connection:
            market_connection.execute("BEGIN")
            yield market_connection
    else:
        yield None


def collect_proposal_inputs(
    connection: sqlite3.Connection,
    *,
    tickers: tuple[str, ...],
    proposal_id: str,
    now: datetime,
    sqlite_path: Path,
    budget_min_yen: int,
    budget_max_yen: int,
    market_connection: sqlite3.Connection | None = None,
    original_buys: dict[str, _ReportedBuy] | None = None,
) -> ProposalInputs:
    """台帳・quoteを一度収集し、保有の起案時だけ対象の照合済み買付元も同じ読取内で渡す。"""
    if not connection.in_transaction:
        raise ProposalError("proposal input collection requires an existing transaction")
    if now.utcoffset() is None:
        raise ProposalError("proposal clock requires a timezone")
    ledger, head = load_ledger_in_transaction(connection)
    if ledger.as_of > now or any(event.occurred_at > now for event in ledger.events):
        raise ProposalError("ledger contains facts after the proposal instant")
    state = replay_events_through(ledger.events, now)
    held_tickers = {ticker for ticker, lots in state.lots.items() if lots}
    relevant_tickers = tuple(sorted(set(tickers) | held_tickers))
    quotes: list[QuoteInput] = []
    quantity_basis: dict[str, bool] = {}
    prices: dict[str, MarketPrice] = {}
    with proposal_market_snapshot(sqlite_path, connection=market_connection) as market_connection:
        if market_connection is not None:
            try:
                require_closed_holding_basis(ledger, now=now, connection=market_connection)
            except UnresolvedHoldingQuantityError as error:
                raise ProposalError(str(error)) from error
            for ticker in relevant_tickers:
                if ticker in held_tickers:
                    observed, confirmed = holding_quote(
                        ledger,
                        ticker=ticker,
                        sqlite_path=sqlite_path,
                        now=now,
                        connection=market_connection,
                    )
                    quantity_basis[ticker] = confirmed
                    price = holding_market_price(
                        ticker=ticker, quote=observed, basis_confirmed=confirmed, now=now
                    )
                    if price is not None:
                        prices[ticker] = price
                else:
                    observed = read_unadjusted_close(
                        sqlite_path=sqlite_path, ticker=ticker, at=now, connection=market_connection
                    )
                if observed is not None:
                    quotes.append(
                        QuoteInput(
                            ticker=ticker,
                            price_yen=finite_decimal(observed.close_yen),
                            price_as_of=observed.price_as_of,
                            price_basis="last_close_unadjusted",
                            source_ref="jquants_daily_bars",
                        )
                    )
        # Classification must not observe a later correction than the saved quotes.
        master = (
            None
            if market_connection is None or not relevant_tickers
            else read_master_snapshot(
                sqlite_path,
                as_of=now.astimezone(JST).date(),
                tickers=relevant_tickers,
                connection=market_connection,
            )
        )
    portfolio = summarize_portfolio(ledger, state, prices)
    positions: list[PositionInput] = []
    for holding in portfolio.holdings:
        bought, _ = holding_episode(ledger, holding.ticker, now)
        original_ref, unavailable, origin = _original_assessment(
            connection, ticker=holding.ticker, bought=bought
        )
        if original_buys is not None and holding.ticker in tickers and origin is not None:
            original_buys[holding.ticker] = origin
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
    classifications: list[SecurityClassification] = []
    if master is not None:
        for ticker in relevant_tickers:
            security = master.securities.get(ticker)
            if security is not None:
                classifications.append(
                    SecurityClassification(
                        ticker=ticker,
                        market_segment=security.market_segment,
                        is_common_stock=security.is_common_stock,
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


def proposal_liquidity_context(
    selection: ResearchSelection,
) -> tuple[LiquidityContext | GeneratedLiquidityContext, ...]:
    if isinstance(selection, GeneratedSelection):
        generation = selection.inputs.generation
        generated: list[LiquidityContext | GeneratedLiquidityContext] = []
        for target in selection.targets:
            model = generation.result("market_context", target.ticker)
            price = None if model is None else model.payload.get("price")
            try:
                adv = (
                    finite_decimal(
                        price.get("avg_turnover_20d_oku") if isinstance(price, dict) else None
                    )
                    * 100_000_000
                )
            except ValueError:
                adv = None
            generated.append(
                GeneratedLiquidityContext(
                    ticker=target.ticker,
                    generation_id=generation.generation_id,
                    analysis_as_of=generation.as_of,
                    calculation_revision=None if model is None else model.calculation_revision,
                    price_window_end=price.get("resolved_date")
                    if isinstance(price, dict)
                    else None,
                    turnover_window=price.get("turnover_window")
                    if isinstance(price, dict)
                    else None,
                    adv_yen=adv,
                    unavailable_reason=None if adv is not None else "保存モデルにADV値が無い",
                )
            )
        return tuple(generated)
    snapshot = selection.inputs.analysis
    result: list[LiquidityContext | GeneratedLiquidityContext] = []
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


def _reported_buy_proposal_records(
    connection: sqlite3.Connection,
    original_buys: dict[str, _ReportedBuy],
    *,
    now: datetime,
) -> dict[str, JsonValue]:
    holdings: list[JsonValue] = []
    result: dict[str, JsonValue] = {
        "role": "reported_buy_reference_records_not_current_judgment",
        "recorded_through": now.isoformat(),
        "holdings": holdings,
    }
    for ticker, (reference, proposal) in sorted(original_buys.items()):
        row: dict[str, JsonValue] = {
            "ticker": ticker,
            "decision_reference": reference,
            "proposal_ref": {
                "proposal_id": proposal.proposal_id,
                "draft_sha256": proposal.review.draft_sha256,
            },
            "scope": "whole_proposal",
            "status": "available",
            "records": None,
            "unavailable_reason": None,
        }
        try:
            records = proposal_records(connection, proposal)
        except (ValueError, FileNotFoundError) as error:
            row.update(status="unavailable", unavailable_reason=str(error))
        else:
            row["records"] = {
                kind: [
                    record
                    for record in values
                    if datetime.fromisoformat(str(record["recorded_at"])) <= now
                ]
                for kind, values in (
                    ("corrections", records["corrections"]),
                    ("human_reports", records["human_reports"]),
                )
            }
        holdings.append(row)
    return result


def _previous_buy_context(
    connection: sqlite3.Connection,
    scope: ProposalResearchScope,
    inputs: ProposalInputs,
    *,
    proposal_id: str | None,
    now: datetime,
) -> dict[str, JsonValue]:
    held = {position.ticker for position in inputs.positions if position.quantity > 0}
    tickers = tuple(t.ticker for t in scope.targets if t.purpose == "new" and t.ticker not in held)
    rows: dict[str, JsonValue] = {}
    for ticker, prior in previous_buy_proposals(
        connection, tickers=tickers, known_at=now, exclude_proposal_id=proposal_id
    ).items():
        if prior.status != "available":
            rows[ticker] = {"status": prior.status, "unavailable_reason": prior.reason}
            continue
        proposal, index = prior.proposal, prior.item_index
        assert proposal is not None
        assert index is not None
        item = proposal.items[index]
        assert item.action == "buy"
        assert item.ticker == ticker
        quote = next((q for q in proposal.inputs.quotes if q.ticker == ticker), None)
        rows[ticker] = {
            "status": "available",
            "proposal_ref": {
                "proposal_id": proposal.proposal_id,
                "draft_sha256": proposal.review.draft_sha256,
            },
            "item_index": index,
            "proposal_item_count": len(proposal.items),
            "as_of": proposal.as_of.isoformat(),
            "published_at": proposal.published_at.isoformat(),
            "assessment_ref": item.assessment_ref.model_dump(mode="json"),
            "previous_timing": item.timing,
            "previous_terms": None
            if item.terms is None
            else item.terms.model_dump(
                mode="json", include={"target_session", "limit_price_yen", "quantity", "conditions"}
            ),
            "previous_proposed_limit_yen": str(item.proposed_limit_yen)
            if item.terms is None and item.proposed_limit_yen is not None
            else None,
            "terms_unavailable_reasons": [
                reason.model_dump(mode="json") for reason in item.terms_unavailable_reasons
            ],
            "previous_quote": None if quote is None else quote.model_dump(mode="json"),
            "previous_rationale": item.rationale,
            "previous_review_conditions": list(item.review_conditions),
            "human_report_scope": "whole_proposal",
            "human_reports": [report.model_dump(mode="json") for report in prior.reports],
        }
    return {
        "role": "previous_published_buy_not_current_terms_or_execution",
        "recorded_through": now.isoformat(),
        "targets": rows,
    }


@dataclass(frozen=True, slots=True)
class ProposalAuthoringContext:
    operation_id: str
    proposal_id: str | None
    request_scope: RequestScope
    research_scope: ProposalResearchScope
    inputs: ProposalInputs
    assessments: dict[str, ReviewedAssessment]
    liquidity_context: tuple[LiquidityContext | GeneratedLiquidityContext, ...]
    market_context_assessment_id: str | None
    latest_assessment_ids: dict[str, str | None]
    next_order_session: date | None
    calculations: dict[str, dict[str, object]]
    saved_market_context_assessment: JsonValue
    saved_analysis_context: JsonValue
    saved_selection_context: dict[str, JsonValue]
    saved_discovery_context: dict[str, JsonValue] | None
    saved_profit_context: dict[str, JsonValue] | None
    reported_buy_proposal_records: dict[str, JsonValue]
    listing_designations: ListingDesignationCheck
    tender_filing_references: TenderFilingLookup | None
    previous_buy_context: dict[str, JsonValue]

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
            "saved_market_context_assessment": deepcopy(self.saved_market_context_assessment),
            "saved_analysis_context": deepcopy(self.saved_analysis_context),
            "saved_selection_context": deepcopy(self.saved_selection_context),
            "saved_discovery_context": deepcopy(self.saved_discovery_context),
            "saved_profit_context": deepcopy(self.saved_profit_context),
            "reported_buy_proposal_records": deepcopy(self.reported_buy_proposal_records),
            "previous_buy_context": deepcopy(self.previous_buy_context),
            "listing_designations": self.listing_designations.model_dump(mode="json"),
            "tender_filing_references": self.tender_filing_references.model_dump(mode="json")
            if self.tender_filing_references is not None
            else None,
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
    market_connection: sqlite3.Connection | None,
) -> dict[str, dict[str, object]]:
    from baibai_engine.investment_judgment.stock_assessment.document import current_price_projection
    from baibai_engine.investment_judgment.trade_proposal.conditions import (
        assessment_quote_basis,
        buy_evaluation_payload,
        buy_evaluator,
    )
    from baibai_engine.portfolio.holdings.price_grid import floor_common_stock_limit

    result: dict[str, dict[str, object]] = {}
    for outcome in scope.outcomes:
        if outcome.status != "reviewed":
            continue
        pair = assessments[outcome.assessment_ref.assessment_id]
        quote = next((quote for quote in inputs.quotes if quote.ticker == outcome.ticker), None)
        row: dict[str, object] = {
            "current_quote_projection": None,
            "case_reassessment_required": outcome.purpose == "holding",
        }
        if outcome.purpose == "holding":
            basis = assessment_quote_basis(
                pair.document,
                quote,
                sqlite_path=sqlite_path,
                now=now,
                market_connection=market_connection,
            )
            row["current_quote_projection"] = current_price_projection(
                pair.document,
                price_yen=None if quote is None else quote.price_yen,
                price_as_of=None if quote is None else quote.price_as_of,
                as_of=now.astimezone(JST).date(),
                basis_confirmed=basis and latest[outcome.ticker] == pair.assessment_id,
                max_quote_age_days=PORTFOLIO_POLICY["valuation"]["market_price_max_age_days"],
            )
        elif quote is not None:
            evaluate = buy_evaluator(
                pair,
                inputs=inputs,
                target_session=session,
                latest=latest[outcome.ticker] == pair.assessment_id,
                sqlite_path=sqlite_path,
                now=now,
                market_connection=market_connection,
            )
            at_quote = evaluate(quote.price_yen, None)
            # 次の立会で新たに取得できない給付を終価に入れない。元評価の値はそのまま。
            row["current_quote_projection"] = at_quote.current_price_projection
            row["buy_at_actual_quote"] = buy_evaluation_payload(at_quote)
            maximum = at_quote.maximum_price_yen
            if maximum is not None and maximum > 0:
                row["calculation_at_maximum_price"] = buy_evaluation_payload(
                    evaluate(maximum, None)
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
    from baibai_engine.portfolio.holdings.market_observations import next_order_session

    with (
        closing(connect_read_only(db_path)) as connection,
        proposal_market_snapshot(sqlite_path) as market_connection,
    ):
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
        original_buys: dict[str, _ReportedBuy] = {}
        inputs = collect_proposal_inputs(
            connection,
            tickers=tuple(target.ticker for target in scope.targets),
            proposal_id=proposal_id or "",
            now=now,
            sqlite_path=sqlite_path,
            budget_min_yen=DEFAULT_BUDGET_MIN_YEN,
            budget_max_yen=DEFAULT_BUDGET_MAX_YEN,
            market_connection=market_connection,
            original_buys=original_buys if scope.targets[0].purpose == "holding" else None,
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
        saved_context = saved_market_assessment(selection)
        context_id = saved_context.get("context_id") if isinstance(saved_context, dict) else None
        if context_id is not None and not isinstance(context_id, str):
            raise ProposalError("saved Market Context Assessment identity must be text")
        session = next_order_session(sqlite_path=sqlite_path, now=now, connection=market_connection)
        listing = read_listing_designations(
            market_connection,
            tickers=tuple(target.ticker for target in scope.targets),
            known_at=now,
        )
        flagged = tuple(t for t, flags in (listing.designations_by_ticker or {}).items() if flags)
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
                market_connection=market_connection,
            ),
            saved_context,
            project_generated_inputs(selection.inputs.generation, proposal_view=True)
            if isinstance(selection, GeneratedSelection)
            else None,
            {
                "role": "selection_time_rationale_not_final_judgment",
                "selection_sha256": scope.selection_sha256,
                "research_as_of": selection.research_as_of.isoformat(),
                "input_known_at": selection.input_known_at.isoformat(),
                "request": selection.request.model_dump(mode="json"),
                "selection_summary": selection.selection_summary,
                "targets": [target.model_dump(mode="json") for target in selection.targets],
            },
            saved_discovery_context(selection, selection_hash=scope.selection_sha256),
            saved_profit_context(selection, selection_hash=scope.selection_sha256),
            _reported_buy_proposal_records(connection, original_buys, now=now),
            listing,
            read_tender_filing_references(market_connection, tickers=flagged, known_at=now),
            _previous_buy_context(connection, scope, inputs, proposal_id=proposal_id, now=now),
        )
