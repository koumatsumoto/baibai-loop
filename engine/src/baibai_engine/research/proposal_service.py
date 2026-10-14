"""保存入力・独立Review・現在gateを照合して、提案だけを原子的に発行する。"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from pathlib import Path

from baibai_engine.foundation.repository_layout import MARKET_DB_PATH
from baibai_engine.foundation.time import JST
from baibai_engine.market.universe import ELIGIBLE_MARKETS, TSE_33_SECTORS
from baibai_engine.position.market_source import next_order_session, quantity_basis_is_confirmed
from baibai_engine.position.policy import PORTFOLIO_POLICY
from baibai_engine.position.price_grid import PRICE_GRID_CONFIRMED_AT, PRICE_GRID_VALID_UNTIL
from baibai_engine.research.assessment import StockAssessmentDocument
from baibai_engine.research.assessment_store import (
    ReviewedAssessment,
    latest_assessment_id,
    require_assessment_ref,
)
from baibai_engine.research.entry_policy import BuyTermsEvaluation, evaluate_buy_terms
from baibai_engine.research.holding_policy import evaluate_holding_proposal
from baibai_engine.research.operation.research_binding import require_proposal_research_scope
from baibai_engine.research.proposal import (
    BuyItem,
    GateReason,
    HoldItem,
    InvestmentProposal,
    ProposalConflictError,
    ProposalError,
    ProposalInputs,
    QuoteInput,
    SellItem,
    proposal_draft_sha256,
)
from baibai_engine.research.proposal_fingerprint import proposal_inputs_fingerprint
from baibai_engine.research.proposal_inputs import (
    DEFAULT_BUDGET_MAX_YEN,
    DEFAULT_BUDGET_MIN_YEN,
    collect_proposal_inputs,
    proposal_liquidity_context,
)
from baibai_engine.research.proposal_policy import validate_proposal_policy
from baibai_engine.research.proposal_query import load_investment_proposal
from baibai_engine.research.proposal_warnings import build_proposal_warnings
from baibai_engine.storage.application.json import canonical_json
from baibai_engine.storage.application.read import connect_read_only
from baibai_engine.storage.application.write import connect_rw, initialize_database


def _quote(inputs: ProposalInputs, ticker: str) -> QuoteInput | None:
    return next((quote for quote in inputs.quotes if quote.ticker == ticker), None)


def assessment_quote_basis(
    assessment: StockAssessmentDocument, quote: QuoteInput | None, *, sqlite_path: Path
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
        and quote.basis_confirmed
        and price_fact is not None
        and quantity_basis_is_confirmed(
            sqlite_path=sqlite_path,
            ticker=quote.ticker,
            from_date=price_fact.as_of,
            through_date=quote.price_as_of,
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
) -> BuyTermsEvaluation:
    quote = _quote(inputs, pair.document.input_snapshot.ticker)
    ticker = pair.document.input_snapshot.ticker
    return evaluate_buy_terms(
        pair.document,
        reviewed=True,
        latest=latest,
        as_of=now.astimezone(JST).date(),
        quote=quote,
        proposed_limit_yen=proposed_limit_yen,
        basis_confirmed=assessment_quote_basis(pair.document, quote, sqlite_path=sqlite_path),
        minimum_required_annual_return_pct=Decimal(
            str(PORTFOLIO_POLICY["valuation"]["minimum_required_annual_return_pct"])
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
        "current_price_above_maximum": result.current_price_above_maximum,
        "current_price_projection": result.current_price_projection,
        "limit_price_projection": result.limit_price_projection,
    }


def terms_unavailable_reasons(
    item: BuyItem | SellItem, inputs: ProposalInputs, *, sqlite_path: Path, now: datetime
) -> tuple[GateReason, ...]:
    """経済判断と独立した、具体注文条件の不足を現在入力から列挙する。"""
    reasons: list[GateReason] = []
    quote = _quote(inputs, item.ticker)
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
        or classification.sector_33 not in TSE_33_SECTORS
    ):
        reasons.append(
            GateReason(
                code="security_classification_unavailable",
                message="対象が既存分類の東証普通株と確認できません。",
            )
        )
    session = next_order_session(sqlite_path=sqlite_path, now=now)
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


def _limit_for_partial_buy(item: BuyItem) -> Decimal:
    if item.terms is not None:
        return item.terms.limit_price_yen
    assert item.proposed_limit_yen is not None
    return item.proposed_limit_yen


def _validate_action(
    connection: sqlite3.Connection,
    proposal: InvestmentProposal,
    item: BuyItem | HoldItem | SellItem,
    pair: ReviewedAssessment,
    *,
    sqlite_path: Path,
    now: datetime,
) -> tuple[GateReason, ...]:
    latest = latest_assessment_id(connection, item.ticker) == pair.assessment_id
    inputs = proposal.inputs
    blockers: list[GateReason] = []
    if item.action == "buy":
        result = buy_evaluation(
            pair,
            inputs=inputs,
            proposed_limit_yen=_limit_for_partial_buy(item),
            latest=latest,
            sqlite_path=sqlite_path,
            now=now,
        )
        blockers.extend(result.hard_blockers)
        if item.terms is not None and (
            item.terms.quantity != result.quantity
            or item.terms.maximum_entry_price_yen != result.maximum_price_yen
            or item.terms.valuation_as_of != pair.document.input_snapshot.as_of
        ):
            blockers.append(
                GateReason(
                    code="buy_terms_calculation_differs",
                    message="買付条件が保存評価と現在入力の純粋算術へ一致しません。",
                )
            )
    else:
        quote = _quote(inputs, item.ticker)
        quote = (
            None
            if quote is None
            else quote.model_copy(
                update={
                    "basis_confirmed": assessment_quote_basis(
                        pair.document, quote, sqlite_path=sqlite_path
                    )
                }
            )
        )
        result_holding = evaluate_holding_proposal(
            pair.document,
            position=item.position,
            as_of=now.astimezone(JST).date(),
            quote=quote,
            remaining_reward=None
            if item.action == "hold" and item.holding_basis == "investment_case_intact"
            else item.remaining_reward,
            case_reassessment_reason=item.case_reassessment_reason,
            primary_verified=pair.review.primary_source_check == "verified",
            reviewed=True,
            latest=latest,
            max_quote_age_days=PORTFOLIO_POLICY["valuation"]["market_price_max_age_days"],
        )
        blockers.extend(result_holding.hard_blockers)
        if (
            result_holding.action != item.action
            or (item.action == "hold" and result_holding.holding_basis != item.holding_basis)
            or (item.action == "sell" and result_holding.reason_code != item.reason_code)
        ):
            blockers.append(
                GateReason(
                    code="holding_judgment_differs",
                    message="保有行動・根拠が今回の仮説確認/残存見返り判断へ一致しません。",
                )
            )
    if item.action in {"buy", "sell"}:
        unavailable = terms_unavailable_reasons(item, inputs, sqlite_path=sqlite_path, now=now)
        if item.terms is None:
            if {reason.code for reason in unavailable} != {
                reason.code for reason in item.terms_unavailable_reasons
            }:
                blockers.append(
                    GateReason(
                        code="terms_unavailable_reasons_differ",
                        message="具体条件の不足理由が現在入力へ一致しません。",
                    )
                )
        else:
            blockers.extend(unavailable)
            if item.terms.target_session != next_order_session(sqlite_path=sqlite_path, now=now):
                blockers.append(
                    GateReason(
                        code="target_session_not_next_available",
                        message="提案条件のsessionが次の注文可能sessionへ一致しません。",
                    )
                )
            if item.terms.expires_at <= now:
                blockers.append(
                    GateReason(
                        code="target_session_not_next_available",
                        message="提案条件の期限を経過しました。",
                    )
                )
    return tuple(blockers)


@dataclass(frozen=True, slots=True)
class ProposalValidation:
    valid: bool
    draft_sha256: str
    blockers: tuple[GateReason, ...]

    def payload(self) -> dict[str, object]:
        return {
            "status": "valid" if self.valid else "blocked",
            "valid": self.valid,
            "draft_sha256": self.draft_sha256,
            "blockers": [reason.model_dump(mode="json") for reason in self.blockers],
        }


def check_proposal(
    connection: sqlite3.Connection,
    proposal: InvestmentProposal,
    *,
    sqlite_path: Path,
    now: datetime,
    require_review: bool = True,
) -> ProposalValidation:
    proposal = InvestmentProposal.model_validate(proposal.model_dump(mode="python"))
    validate_proposal_policy(proposal)
    if now.utcoffset() is None:
        raise ProposalError("proposal clock requires a timezone")
    try:
        selection = require_proposal_research_scope(
            connection,
            scope=proposal.research_scope,
            request_scope=proposal.request_scope,
            published_at=now,
        )
    except ValueError as error:
        raise ProposalConflictError(str(error)) from error
    if proposal.prepared_at < selection.input_known_at:
        raise ProposalConflictError("proposal preparation precedes its saved Research inputs")
    if proposal.liquidity_context != proposal_liquidity_context(selection):
        raise ProposalConflictError("proposal liquidity differs from the saved selection snapshot")
    market_context = selection.inputs.market_context.get("saved_market_context_assessment")
    context_id = market_context.get("context_id") if isinstance(market_context, dict) else None
    if proposal.market_context_assessment_id != context_id:
        raise ProposalConflictError(
            "proposal Market Context Assessment differs from saved selection"
        )
    actual = collect_proposal_inputs(
        connection,
        tickers=tuple(target.ticker for target in proposal.research_scope.targets),
        proposal_id=proposal.proposal_id,
        now=now,
        sqlite_path=sqlite_path,
        budget_min_yen=DEFAULT_BUDGET_MIN_YEN,
        budget_max_yen=DEFAULT_BUDGET_MAX_YEN,
    )
    if proposal_inputs_fingerprint(actual) != proposal_inputs_fingerprint(proposal.inputs):
        raise ProposalConflictError(
            "proposal input facts changed; resnapshot and independently review"
        )
    draft_hash = proposal_draft_sha256(proposal)
    if require_review and proposal.review.draft_sha256 != draft_hash:
        raise ProposalConflictError(
            f"proposal content review binds a different draft; expected {draft_hash}"
        )
    pairs: dict[str, ReviewedAssessment] = {}
    blockers: list[GateReason] = []
    if proposal.as_of != now.astimezone(JST).date():
        blockers.append(
            GateReason(
                code="proposal_basis_requires_refresh",
                message="新規発行する提案の基準日は本日である必要があります。",
            )
        )
    if (
        not proposal.prepared_at <= proposal.review.reviewed_at <= now
        or proposal.inputs.checked_at > now
    ):
        blockers.append(
            GateReason(
                code="proposal_time_after_publish",
                message="準備・入力収集・独立Reviewの時刻を現在発行時刻までに揃えてください。",
            )
        )
    for item in proposal.items:
        if item.assessment_ref is None:
            continue
        pair = require_assessment_ref(connection, item.assessment_ref)
        pairs[pair.assessment_id] = pair
        if pair.document.input_snapshot.ticker != item.ticker:
            raise ProposalConflictError("proposal item and exact assessment ticker differ")
        published_at = connection.execute(
            "SELECT published_at FROM stock_assessment WHERE assessment_id=?", (pair.assessment_id,)
        ).fetchone()
        if datetime.fromisoformat(str(published_at[0])) > now:
            blockers.append(
                GateReason(
                    code="assessment_after_proposal",
                    message="参照評価の公開が今回提案時刻より後です。",
                )
            )
        if item.action in {"buy", "hold", "sell"}:
            blockers.extend(
                _validate_action(connection, proposal, item, pair, sqlite_path=sqlite_path, now=now)
            )
    warnings = build_proposal_warnings(
        proposal.inputs,
        proposal.items,
        proposal.liquidity_context,
        assessments={identity: pair.document for identity, pair in pairs.items()},
    )
    if (
        proposal.warnings != warnings.warnings
        or proposal.portfolio_exposure != warnings.portfolio_exposure
    ):
        raise ProposalConflictError(
            "proposal warning/exposure projection differs from fixed inputs"
        )
    for item in proposal.items:
        if (
            item.action in {"buy", "sell"}
            and item.terms is not None
            and item.terms.warnings != warnings.terms_warnings.get(item.ticker, ())
        ):
            raise ProposalConflictError("proposal terms warnings differ from fixed inputs")
    return ProposalValidation(not blockers, draft_hash, tuple(blockers))


class InvestmentProposalService:
    def __init__(
        self,
        db_path: Path | None = None,
        *,
        sqlite_path: Path = MARKET_DB_PATH,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._db_path = db_path
        self._sqlite_path = sqlite_path
        self._clock = clock or (lambda: datetime.now(JST))

    def check(
        self, proposal: InvestmentProposal, *, require_review: bool = True
    ) -> ProposalValidation:
        with closing(connect_read_only(self._db_path)) as connection:
            connection.execute("BEGIN")
            return check_proposal(
                connection,
                proposal,
                sqlite_path=self._sqlite_path,
                now=self._clock(),
                require_review=require_review,
            )

    def publish(self, proposal: InvestmentProposal) -> InvestmentProposal:
        # Revalidate even callers using model_copy/model_construct; these APIs bypass validators.
        proposal = InvestmentProposal.model_validate(proposal.model_dump(mode="python"))
        now = self._clock()
        initialize_database(self._db_path)
        with closing(connect_rw(self._db_path)) as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                existing = connection.execute(
                    "SELECT 1 FROM investment_proposal WHERE proposal_id=?", (proposal.proposal_id,)
                ).fetchone()
                if existing is not None:
                    stored = load_investment_proposal(connection, proposal.proposal_id)
                    if canonical_json(
                        stored.model_dump(mode="json", exclude={"published_at"})
                    ) != canonical_json(proposal.model_dump(mode="json", exclude={"published_at"})):
                        raise ProposalConflictError(
                            "immutable investment proposal differs from its existing publication"
                        )
                    connection.rollback()
                    return stored
                validation = check_proposal(
                    connection, proposal, sqlite_path=self._sqlite_path, now=now
                )
                if not validation.valid:
                    raise ProposalError("; ".join(reason.message for reason in validation.blockers))
                published = proposal.model_copy(update={"published_at": now})
                connection.execute(
                    "INSERT INTO investment_proposal(proposal_id,as_of,prepared_at,published_at,"
                    "request_scope,payload,draft_sha256) VALUES (?,?,?,?,?,?,?)",
                    (
                        published.proposal_id,
                        published.as_of.isoformat(),
                        published.prepared_at.isoformat(),
                        published.published_at.isoformat(),
                        published.request_scope,
                        canonical_json(published.payload()),
                        validation.draft_sha256,
                    ),
                )
                connection.executemany(
                    "INSERT INTO investment_proposal_item(proposal_id,item_index,ticker,action,"
                    "assessment_id) VALUES (?,?,?,?,?)",
                    [
                        (
                            published.proposal_id,
                            index,
                            item.ticker,
                            item.action,
                            None
                            if item.assessment_ref is None
                            else item.assessment_ref.assessment_id,
                        )
                        for index, item in enumerate(published.items)
                    ],
                )
                for item in published.items:
                    if item.action == "buy" and item.terms is not None:
                        connection.execute(
                            "INSERT INTO position_decision_reference(reference,ticker,side,origin,"
                            "proposal_id,source_id,source_digest) "
                            "VALUES (?,?,'buy','proposal',?,?,?)",
                            (
                                published.proposal_id,
                                item.ticker,
                                published.proposal_id,
                                published.proposal_id,
                                validation.draft_sha256,
                            ),
                        )
                stored = load_investment_proposal(connection, published.proposal_id)
                connection.commit()
                return stored
            except BaseException:
                connection.rollback()
                raise
