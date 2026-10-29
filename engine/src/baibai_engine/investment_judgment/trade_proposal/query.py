"""不変の提案を読み、保存条件と現在の再試算を別の値として返す。"""

from __future__ import annotations

import sqlite3
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Literal

from baibai_engine.investment_judgment.stock_assessment.store import (
    ReviewedAssessment,
    require_assessment_ref,
)
from baibai_engine.investment_judgment.trade_proposal.contracts import (
    BuyItem,
    GateReason,
    HoldItem,
    ProposalConflictError,
    ProposalInputs,
    SellItem,
    TradeProposal,
    order_readiness,
    proposal_draft_sha256,
)
from baibai_engine.storage.sqlite.application.read import connect_read_only


def has_published_proposal_for_operation(connection: sqlite3.Connection, operation_id: str) -> bool:
    """Operationへの参照束縛前も含め、公開済み提案の存在だけを確認する。"""
    return (
        connection.execute(
            "SELECT 1 FROM investment_proposal "
            "WHERE json_extract(payload, '$.research_scope.operation_id') = ? LIMIT 1",
            (operation_id,),
        ).fetchone()
        is not None
    )


def load_investment_proposal(connection: sqlite3.Connection, proposal_id: str) -> TradeProposal:
    """本文/hash/index/正確な評価pairを検証する。現在の資格やprogressは読まない。"""

    row = connection.execute(
        "SELECT as_of,prepared_at,published_at,request_scope,payload,draft_sha256 "
        "FROM investment_proposal WHERE proposal_id=?",
        (proposal_id,),
    ).fetchone()
    if row is None:
        raise FileNotFoundError(f"investment proposal is unavailable: {proposal_id}")
    proposal = TradeProposal.model_validate_json(str(row[4]))
    draft_hash = proposal_draft_sha256(proposal)
    if (
        proposal.proposal_id != proposal_id
        or proposal.as_of.isoformat() != str(row[0])
        or proposal.prepared_at != datetime.fromisoformat(str(row[1]))
        or proposal.published_at != datetime.fromisoformat(str(row[2]))
        or proposal.request_scope != str(row[3])
        or draft_hash != str(row[5])
        or draft_hash != proposal.review.draft_sha256
    ):
        raise ProposalConflictError("stored proposal identity/hash differs from its payload")
    actual_items = connection.execute(
        "SELECT item_index,ticker,action,assessment_id FROM investment_proposal_item "
        "WHERE proposal_id=? ORDER BY item_index",
        (proposal_id,),
    ).fetchall()
    expected_items = [
        (
            index,
            item.ticker,
            item.action,
            None if item.assessment_ref is None else item.assessment_ref.assessment_id,
        )
        for index, item in enumerate(proposal.items)
    ]
    if [tuple(item) for item in actual_items] != expected_items:
        raise ProposalConflictError("stored proposal item index differs from its payload")
    for item in proposal.items:
        if item.assessment_ref is not None:
            pair = require_assessment_ref(connection, item.assessment_ref)
            if pair.document.input_snapshot.ticker != item.ticker:
                raise ProposalConflictError("stored proposal assessment ticker differs")
    for position in proposal.inputs.positions:
        if position.original_assessment_ref is not None:
            pair = require_assessment_ref(connection, position.original_assessment_ref)
            if pair.document.input_snapshot.ticker != position.ticker:
                raise ProposalConflictError("stored original assessment ticker differs")
    binding = connection.execute(
        "SELECT reference,ticker,side,origin,proposal_id,source_id,source_digest "
        "FROM position_decision_reference WHERE proposal_id=?",
        (proposal_id,),
    ).fetchall()
    expected_bindings = [
        (proposal_id, item.ticker, "buy", "proposal", proposal_id, proposal_id, draft_hash)
        for item in proposal.items
        if item.action == "buy" and item.terms is not None
    ]
    if [tuple(item) for item in binding] != expected_bindings:
        raise ProposalConflictError("stored proposal buy binding differs from its concrete item")
    return proposal


def get_investment_proposal(*, proposal_id: str, db_path: Path | None = None) -> TradeProposal:
    with closing(connect_read_only(db_path)) as connection:
        connection.execute("BEGIN")
        return load_investment_proposal(connection, proposal_id)


def load_latest_investment_proposal(connection: sqlite3.Connection) -> TradeProposal | None:
    """全scopeから最大の(as_of, aware published_at, ID)を読み、破損版を飛ばさない。"""

    rows = connection.execute(
        "SELECT proposal_id,published_at FROM investment_proposal "
        "WHERE as_of=(SELECT MAX(as_of) FROM investment_proposal)"
    ).fetchall()
    headers: list[tuple[datetime, str]] = []
    for row in rows:
        try:
            instant = datetime.fromisoformat(str(row[1]))
            if instant.utcoffset() is None:
                raise ValueError("stored publication time must include a timezone")
        except ValueError as error:
            raise ProposalConflictError("invalid stored proposal publication header") from error
        headers.append((instant, str(row[0])))
    # Compare aware instants in Python: textual offsets and SQLite julianday
    # respectively lose instant ordering and sub-millisecond precision.
    if not headers:
        return None
    return load_investment_proposal(connection, max(headers)[1])


@dataclass(frozen=True, slots=True)
class CurrentProposalPreview:
    saved_proposal_id: str
    previewed_at: datetime
    status: Literal["unchanged", "changed", "blocked"]
    current_inputs: ProposalInputs
    items: tuple[dict[str, object], ...]
    change_reasons: tuple[GateReason, ...]
    warnings: tuple[str, ...]
    portfolio_exposure: dict[str, object]

    def payload(self) -> dict[str, object]:
        return {
            "saved_proposal_id": self.saved_proposal_id,
            "previewed_at": self.previewed_at.isoformat(),
            "status": self.status,
            "current_inputs": self.current_inputs.model_dump(mode="json"),
            "items": list(self.items),
            "change_reasons": [reason.model_dump(mode="json") for reason in self.change_reasons],
            "warnings": list(self.warnings),
            "portfolio_exposure": self.portfolio_exposure,
        }


def _preview_buy(
    item: BuyItem,
    pair: ReviewedAssessment,
    inputs: ProposalInputs,
    *,
    latest: bool,
    sqlite_path: Path,
    now: datetime,
    market_connection: sqlite3.Connection | None,
) -> tuple[dict[str, object], list[GateReason]]:
    from baibai_engine.investment_judgment.trade_proposal.conditions import (
        buy_evaluation,
        buy_evaluation_payload,
    )
    from baibai_engine.portfolio.holdings.market_observations import next_order_session
    from baibai_engine.portfolio.holdings.price_grid import floor_common_stock_limit

    ceiling = item.terms.limit_price_yen if item.terms is not None else item.proposed_limit_yen
    assert ceiling is not None
    result = buy_evaluation(
        pair,
        inputs=inputs,
        proposed_limit_yen=ceiling,
        latest=latest,
        sqlite_path=sqlite_path,
        now=now,
        market_connection=market_connection,
    )
    blockers = list(result.hard_blockers)
    if item.terms is not None and item.terms.notional_yen > inputs.available_cash_yen:
        blockers.append(
            GateReason(
                code="available_cash_below_board_lot",
                message="保存した買付数量全体を賄うcashが不足しています。",
            )
        )
    quote = next((quote for quote in inputs.quotes if quote.ticker == item.ticker), None)
    trial: dict[str, object] | None = None
    if quote is not None:
        classification = next(
            (row for row in inputs.security_classification if row.ticker == item.ticker), None
        )
        session = next_order_session(sqlite_path=sqlite_path, now=now)
        grid = (
            None
            if classification is None or session is None
            else floor_common_stock_limit(
                quote.price_yen,
                target_session=session,
                market_segment=classification.market_segment,
                is_common_stock=classification.is_common_stock,
            )
        )
        current = buy_evaluation(
            pair,
            inputs=inputs,
            proposed_limit_yen=quote.price_yen if grid is None else grid,
            latest=latest,
            sqlite_path=sqlite_path,
            now=now,
            market_connection=market_connection,
        )
        trial = {
            **buy_evaluation_payload(current),
            "calculation_price_yen": str(quote.price_yen if grid is None else grid),
            "price_grid_available": grid is not None,
        }
    return {
        "current_quote_calculation": trial,
        "saved_limit_revalidation": buy_evaluation_payload(result),
    }, blockers


def _preview_holding(
    item: HoldItem | SellItem,
    pair: ReviewedAssessment,
    inputs: ProposalInputs,
    *,
    latest: bool,
    sqlite_path: Path,
    now: datetime,
    market_connection: sqlite3.Connection | None,
) -> tuple[dict[str, object], list[GateReason]]:
    from baibai_engine.investment_judgment.trade_proposal.conditions import assessment_quote_basis
    from baibai_engine.investment_judgment.trade_proposal.holding_policy import (
        evaluate_holding_proposal,
    )
    from baibai_engine.portfolio.policy import PORTFOLIO_POLICY
    from baibai_engine.primitives.time import JST

    position = next(
        (position for position in inputs.positions if position.ticker == item.ticker), None
    )
    if position is None:
        return {"current_quote_calculation": None}, [
            GateReason(code="holding_absent", message="保存した保有銘柄の現在保有がありません。")
        ]
    quote = next((quote for quote in inputs.quotes if quote.ticker == item.ticker), None)
    result = evaluate_holding_proposal(
        pair.document,
        position=position,
        as_of=now.astimezone(JST).date(),
        quote=quote,
        price_basis_confirmed=assessment_quote_basis(
            pair.document, quote, sqlite_path=sqlite_path, market_connection=market_connection
        ),
        remaining_reward=item.remaining_reward,
        case_reassessment_reason=item.case_reassessment_reason,
        primary_verified=pair.review.primary_source_check == "verified",
        reviewed=True,
        latest=latest,
        max_quote_age_days=PORTFOLIO_POLICY["valuation"]["market_price_max_age_days"],
    )
    blockers = list(result.hard_blockers)
    if result.action != item.action:
        blockers.append(
            GateReason(
                code="holding_judgment_differs",
                message="保存行動と現在の評価適用結果が異なります。",
            )
        )
    if (
        item.action == "sell"
        and item.terms is not None
        and item.terms.quantity != position.quantity
    ):
        blockers.append(
            GateReason(
                code="quantity_basis_unconfirmed",
                message="保存売却数量は現在の全保有数量へ一致しません。",
            )
        )
    return {
        "current_quote_calculation": {
            "action": result.action,
            "reason_code": result.reason_code,
            "current_price_projection": result.current_price_projection,
            "return_calculation_unavailable_reason": result.return_calculation_unavailable_reason,
            "current_position": position.model_dump(mode="json"),
        }
    }, blockers


def preview_current_terms(
    *, proposal_id: str, db_path: Path | None, sqlite_path: Path, now: datetime
) -> CurrentProposalPreview:
    """保存limitと実quoteの再試算を分ける。再発行/最新runへの付替えは行わない。"""
    from baibai_engine.investment_judgment.stock_assessment.document import current_price_projection
    from baibai_engine.investment_judgment.stock_assessment.store import latest_assessment_id
    from baibai_engine.investment_judgment.trade_proposal.conditions import (
        assessment_quote_basis,
        terms_unavailable_reasons,
    )
    from baibai_engine.investment_judgment.trade_proposal.fingerprint import (
        proposal_inputs_fingerprint,
    )
    from baibai_engine.investment_judgment.trade_proposal.inputs import (
        DEFAULT_BUDGET_MAX_YEN,
        DEFAULT_BUDGET_MIN_YEN,
        collect_proposal_inputs,
        proposal_market_snapshot,
    )
    from baibai_engine.investment_judgment.trade_proposal.warnings import build_proposal_warnings
    from baibai_engine.portfolio.holdings.market_observations import next_order_session
    from baibai_engine.portfolio.policy import PORTFOLIO_POLICY
    from baibai_engine.primitives.time import JST

    if now.utcoffset() is None:
        raise ValueError("proposal preview time requires a timezone")
    with (
        closing(connect_read_only(db_path)) as connection,
        proposal_market_snapshot(sqlite_path) as market_connection,
    ):
        connection.execute("BEGIN")
        proposal = load_investment_proposal(connection, proposal_id)
        inputs = collect_proposal_inputs(
            connection,
            tickers=tuple(item.ticker for item in proposal.items),
            proposal_id=proposal_id,
            now=now,
            sqlite_path=sqlite_path,
            budget_min_yen=DEFAULT_BUDGET_MIN_YEN,
            budget_max_yen=DEFAULT_BUDGET_MAX_YEN,
            market_connection=market_connection,
        )
        changes: list[GateReason] = []
        baseline = proposal_inputs_fingerprint(proposal.inputs)
        if baseline != proposal_inputs_fingerprint(inputs):
            for field in type(inputs).model_fields:
                if (
                    field != "checked_at"
                    and proposal_inputs_fingerprint(
                        proposal.inputs.model_copy(update={field: getattr(inputs, field)})
                    )
                    != baseline
                ):
                    changes.append(
                        GateReason(
                            code=f"input_facts_changed:{field}",
                            message=f"{field} の実入力が保存時から変化しています。",
                        )
                    )
        if proposal.as_of != now.astimezone(JST).date():
            changes.append(
                GateReason(
                    code="proposal_basis_requires_refresh",
                    message="提案の保存基準日と現在の適用日が異なります。",
                )
            )
        blocked = False
        rows: list[dict[str, object]] = []
        assessments = {}
        for item in proposal.items:
            blockers: list[GateReason] = []
            row: dict[str, object] = {
                "ticker": item.ticker,
                "saved_action": item.action,
                "saved_order_readiness": order_readiness(item),
                "saved_terms": item.terms.model_dump(mode="json")
                if item.action in {"buy", "sell"} and item.terms is not None
                else None,
                "current_quote_calculation": None,
                "saved_limit_revalidation": None,
                "current_terms_warnings": [],
            }
            if item.assessment_ref is not None:
                pair = require_assessment_ref(connection, item.assessment_ref)
                assessments[pair.assessment_id] = pair.document
                latest = latest_assessment_id(connection, item.ticker) == pair.assessment_id
                if item.action == "buy":
                    calculation, blockers = _preview_buy(
                        item,
                        pair,
                        inputs,
                        latest=latest,
                        sqlite_path=sqlite_path,
                        now=now,
                        market_connection=market_connection,
                    )
                    row.update(calculation)
                elif item.action in {"hold", "sell"}:
                    calculation, blockers = _preview_holding(
                        item,
                        pair,
                        inputs,
                        latest=latest,
                        sqlite_path=sqlite_path,
                        now=now,
                        market_connection=market_connection,
                    )
                    row.update(calculation)
                elif item.action == "no_action":
                    quote = next(
                        (quote for quote in inputs.quotes if quote.ticker == item.ticker), None
                    )
                    row["current_quote_calculation"] = current_price_projection(
                        pair.document,
                        price_yen=None if quote is None else quote.price_yen,
                        price_as_of=None if quote is None else quote.price_as_of,
                        as_of=now.astimezone(JST).date(),
                        basis_confirmed=assessment_quote_basis(
                            pair.document,
                            quote,
                            sqlite_path=sqlite_path,
                            market_connection=market_connection,
                        ),
                        max_quote_age_days=PORTFOLIO_POLICY["valuation"][
                            "market_price_max_age_days"
                        ],
                    )
            if item.action in {"buy", "sell"}:
                blockers.extend(
                    terms_unavailable_reasons(item, inputs, sqlite_path=sqlite_path, now=now)
                )
                if item.terms is None:
                    blockers.append(
                        GateReason(
                            code="order_terms_unconfirmed",
                            message="保存条件は未確定です。条件を確定した新しい提案とReviewが必要です。",
                        )
                    )
                elif item.terms.target_session != next_order_session(
                    sqlite_path=sqlite_path, now=now
                ):
                    blockers.append(
                        GateReason(
                            code="target_session_not_next_available",
                            message="保存条件の対象日が次の注文可能sessionと異なります。",
                        )
                    )
                if item.action == "sell":
                    row["saved_limit_revalidation"] = {
                        "limit_price_yen": None
                        if item.terms is None
                        else str(item.terms.limit_price_yen),
                        "quantity": None if item.terms is None else item.terms.quantity,
                    }
                revalidation = row["saved_limit_revalidation"]
                if isinstance(revalidation, dict):
                    revalidation["hard_blockers"] = [
                        reason.model_dump(mode="json") for reason in blockers
                    ]
            if blockers:
                blocked = True
                changes.extend(blockers)
            row["blockers"] = [reason.model_dump(mode="json") for reason in blockers]
            rows.append(row)
        warnings = build_proposal_warnings(
            inputs, proposal.items, proposal.liquidity_context, assessments=assessments
        )
        for row in rows:
            row["current_terms_warnings"] = list(
                warnings.terms_warnings.get(str(row["ticker"]), ())
            )
        return CurrentProposalPreview(
            proposal_id,
            now,
            "blocked" if blocked else "changed" if changes else "unchanged",
            inputs,
            tuple(rows),
            tuple(changes),
            warnings.warnings,
            warnings.portfolio_exposure.model_dump(mode="json"),
        )
