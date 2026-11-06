"""保存した提案を現在入力で再試算する。保存読取は現在条件へ依存させない。"""

from __future__ import annotations

import sqlite3
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Literal

from baibai_engine.investment_judgment.stock_assessment.document import current_price_projection
from baibai_engine.investment_judgment.stock_assessment.store import (
    ReviewedAssessment,
    latest_assessment_id,
    require_assessment_ref,
)
from baibai_engine.investment_judgment.trade_proposal import inputs as input_reader
from baibai_engine.investment_judgment.trade_proposal.conditions import (
    assessment_quote_basis,
    buy_evaluation,
    buy_evaluation_payload,
    terms_unavailable_reasons,
)
from baibai_engine.investment_judgment.trade_proposal.contracts import (
    BuyItem,
    GateReason,
    HoldItem,
    ProposalInputs,
    SellItem,
    order_readiness,
)
from baibai_engine.investment_judgment.trade_proposal.fingerprint import proposal_inputs_fingerprint
from baibai_engine.investment_judgment.trade_proposal.holding_policy import (
    evaluate_holding_proposal,
)
from baibai_engine.investment_judgment.trade_proposal.query import (
    load_investment_proposal,
    proposal_records,
)
from baibai_engine.investment_judgment.trade_proposal.record_contracts import ProposalRecords
from baibai_engine.investment_judgment.trade_proposal.warnings import build_proposal_warnings
from baibai_engine.portfolio.holdings.market_observations import next_order_session
from baibai_engine.portfolio.holdings.price_grid import floor_common_stock_limit
from baibai_engine.portfolio.policy import PORTFOLIO_POLICY
from baibai_engine.primitives.time import JST
from baibai_engine.storage.sqlite.application.read import connect_read_only


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
    records: ProposalRecords

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
            "records": self.records,
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
    ceiling = item.terms.limit_price_yen if item.terms is not None else item.proposed_limit_yen
    assert ceiling is not None
    # 保存数量を現在の数量上限へ置換せず、作者が選んだ同じ案を再検証する。
    result = buy_evaluation(
        pair,
        inputs=inputs,
        proposed_limit_yen=ceiling,
        selected_quantity=None if item.terms is None else item.terms.quantity,
        latest=latest,
        sqlite_path=sqlite_path,
        now=now,
        market_connection=market_connection,
    )
    blockers = list(result.hard_blockers)
    quote = next((quote for quote in inputs.quotes if quote.ticker == item.ticker), None)
    trial: dict[str, object] | None = None
    if quote is not None:
        classification = next(
            (row for row in inputs.security_classification if row.ticker == item.ticker), None
        )
        session = next_order_session(sqlite_path=sqlite_path, now=now, connection=market_connection)
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
        proposed_sell_limit_yen=(
            item.terms.limit_price_yen if item.action == "sell" and item.terms is not None else None
        ),
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
        },
        "saved_limit_revalidation": {
            "limit_price_yen": None if item.terms is None else str(item.terms.limit_price_yen),
            "quantity": None if item.terms is None else item.terms.quantity,
            "sell_limit_projection": result.sell_limit_projection,
        }
        if item.action == "sell"
        else None,
    }, blockers


def preview_current_terms(
    *, proposal_id: str, db_path: Path | None, sqlite_path: Path, now: datetime
) -> CurrentProposalPreview:
    """保存limitと実quoteの再試算を分ける。再発行/最新runへの付替えは行わない。"""
    if now.utcoffset() is None:
        raise ValueError("proposal preview time requires a timezone")
    with (
        closing(connect_read_only(db_path)) as connection,
        input_reader.proposal_market_snapshot(sqlite_path) as market_connection,
    ):
        connection.execute("BEGIN")
        proposal = load_investment_proposal(connection, proposal_id)
        inputs = input_reader.collect_proposal_inputs(
            connection,
            tickers=tuple(item.ticker for item in proposal.items),
            proposal_id=proposal_id,
            now=now,
            sqlite_path=sqlite_path,
            budget_min_yen=input_reader.DEFAULT_BUDGET_MIN_YEN,
            budget_max_yen=input_reader.DEFAULT_BUDGET_MAX_YEN,
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
                "saved_limit_revalidation": {
                    "limit_price_yen": None
                    if item.terms is None
                    else str(item.terms.limit_price_yen),
                    "quantity": None if item.terms is None else item.terms.quantity,
                    "sell_limit_projection": None,
                }
                if item.action == "sell"
                else None,
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
                    terms_unavailable_reasons(
                        item,
                        inputs,
                        sqlite_path=sqlite_path,
                        now=now,
                        market_connection=market_connection,
                    )
                )
                if item.terms is None:
                    blockers.append(
                        GateReason(
                            code="order_terms_unconfirmed",
                            message="保存条件は未確定です。条件を確定した新しい提案とReviewが必要です。",
                        )
                    )
                elif item.terms.target_session != next_order_session(
                    sqlite_path=sqlite_path, now=now, connection=market_connection
                ):
                    blockers.append(
                        GateReason(
                            code="target_session_not_next_available",
                            message="保存条件の対象日が次の注文可能sessionと異なります。",
                        )
                    )
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
            proposal_records(connection, proposal_id),
        )
