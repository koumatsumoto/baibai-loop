"""保存した提案を現在入力で再試算する。保存読取は現在条件へ依存させない。"""

from __future__ import annotations

import sqlite3
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime
from functools import cache
from pathlib import Path
from typing import Literal

from baibai_engine.investment_judgment.market_assessment.models import StoredAssessment
from baibai_engine.investment_judgment.market_assessment.query import (
    latest_assessment_from_connection,
)
from baibai_engine.investment_judgment.stock_assessment.document import current_price_projection
from baibai_engine.investment_judgment.stock_assessment.store import (
    ReviewedAssessment,
    latest_assessment_id,
)
from baibai_engine.investment_judgment.trade_proposal import inputs as input_reader
from baibai_engine.investment_judgment.trade_proposal.conditions import (
    assessment_quote_basis,
    buy_evaluation_payload,
    buy_evaluator,
    proposal_classification,
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
from baibai_engine.investment_judgment.trade_proposal.entry_policy import acquisition_blocker
from baibai_engine.investment_judgment.trade_proposal.fingerprint import changed_input_fields
from baibai_engine.investment_judgment.trade_proposal.holding_policy import (
    evaluate_holding_proposal,
)
from baibai_engine.investment_judgment.trade_proposal.query import (
    load_proposal_with_assessments,
    proposal_records,
)
from baibai_engine.investment_judgment.trade_proposal.record_contracts import ProposalRecords
from baibai_engine.investment_judgment.trade_proposal.warnings import build_proposal_warnings
from baibai_engine.portfolio.holdings.market_observations import (
    OrderSessionBounds,
    read_order_session_bounds,
)
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
    market_assessment_context: dict[str, object]

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
            "market_assessment_context": self.market_assessment_context,
        }


def _market_assessment_context(
    record: StoredAssessment | None, *, saved_context_id: str | None, now: datetime
) -> dict[str, object]:
    return {
        "basis": "latest_available_saved_assessment",
        "context_id": None if record is None else record.context_id,
        "saved_context_id": saved_context_id,
        "same_reference": None if record is None else record.context_id == saved_context_id,
        "as_of": None if record is None else record.as_of.isoformat(),
        "known_at": None if record is None else record.document.get("known_at"),
        "published_at": None if record is None else record.published_at.isoformat(),
        "age_as_of_days": (
            None if record is None else (now.astimezone(JST).date() - record.as_of).days
        ),
        "interpretation": (
            "現在利用可能な保存済み市場判断の参照です。経過日は基準日からの日本時間の暦日数で、"
            "情報締切からの経過時間ではありません。参照が同じでも新事実を再確認した証拠ではなく、"
            "このpreviewは市場判断を更新しません。"
        ),
    }


def _preview_buy(
    item: BuyItem,
    pair: ReviewedAssessment,
    inputs: ProposalInputs,
    *,
    latest: bool,
    order_blockers: tuple[GateReason, ...],
    sessions: OrderSessionBounds,
    sqlite_path: Path,
    now: datetime,
    market_connection: sqlite3.Connection | None,
) -> tuple[dict[str, object], list[GateReason]]:
    ceiling = item.terms.limit_price_yen if item.terms is not None else item.proposed_limit_yen
    assert ceiling is not None
    # 保存数量を現在の数量上限へ置換せず、作者が選んだ同じ案を再検証する。
    evaluate = buy_evaluator(
        pair,
        inputs=inputs,
        sessions=sessions,
        latest=latest,
        sqlite_path=sqlite_path,
        now=now,
        market_connection=market_connection,
    )
    result = evaluate(ceiling, None if item.terms is None else item.terms.quantity)
    blockers = list(result.hard_blockers)
    quote = next((quote for quote in inputs.quotes if quote.ticker == item.ticker), None)
    trial: dict[str, object] | None = None
    if quote is not None:
        classification = proposal_classification(inputs, item.ticker)
        session = sessions.confirmed_session
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
        current = evaluate(quote.price_yen if grid is None else grid, None)
        trial = {
            **buy_evaluation_payload(current, order_blockers=order_blockers),
            "calculation_price_yen": str(quote.price_yen if grid is None else grid),
            "price_grid_available": grid is not None,
        }
    calculation = buy_evaluation_payload(result, order_blockers=order_blockers)
    return {
        "current_quote_calculation": trial,
        "saved_limit_revalidation": calculation if item.terms is not None else None,
        "saved_ceiling_calculation": calculation if item.terms is None else None,
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
            pair.document,
            quote,
            sqlite_path=sqlite_path,
            now=now,
            market_connection=market_connection,
        ),
        remaining_reward=item.remaining_reward,
        case_reassessment_reason=item.case_reassessment_reason,
        primary_verified=pair.review.primary_source_check == "verified",
        reviewed=True,
        latest=latest,
        max_quote_age_days=PORTFOLIO_POLICY["valuation"]["market_price_max_age_days"],
        conditional_recovery=item.conditional_recovery if item.action == "sell" else None,
        board_lot=inputs.board_lot,
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
        proposal, verified_pairs = load_proposal_with_assessments(connection, proposal_id)
        records = proposal_records(connection, proposal)
        corrected = any(
            record["erroneous_ref"]["proposal_id"] == proposal_id
            for record in records["corrections"]
        )
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
        current_market = latest_assessment_from_connection(
            connection, as_of=now.astimezone(JST).date(), known_at=now
        )
        if (
            current_market is not None
            and current_market.context_id != proposal.market_context_assessment_id
        ):
            changes.append(
                GateReason(
                    code="market_assessment_revision_changed",
                    message=(
                        "固定参照と現在利用可能な市場判断が異なります。"
                        f"保存参照={proposal.market_context_assessment_id or '未記録'}、"
                        f"現在参照={current_market.context_id}、基準日={current_market.as_of}、"
                        f"情報締切={current_market.document.get('known_at') or '未記録'}、"
                        f"発行={current_market.published_at.isoformat()}。"
                        "経済変化や個社への影響を認定するものではありません。"
                        "保存した行動・価値・価格条件は差し替えず、重要な伝達条件を再確認してください。"
                    ),
                )
            )
        changes.extend(
            GateReason(
                code=f"input_facts_changed:{field}",
                message=f"{field} の実入力が保存時から変化しています。",
            )
            for field in changed_input_fields(proposal.inputs, inputs)
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

        @cache
        def order_sessions() -> OrderSessionBounds:
            return read_order_session_bounds(
                sqlite_path=sqlite_path, now=now, connection=market_connection
            )

        for item in proposal.items:
            blockers = (
                [
                    GateReason(
                        code="proposal_corrected",
                        message="この保存提案は誤記録として訂正済みです。原本は履歴として保持し、現在条件へ適用しません。",
                    )
                ]
                if corrected
                else []
            )
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
            if item.action == "buy":
                row.update(
                    saved_economic_decision=item.economic_decision,
                    saved_proposed_limit_yen=(
                        None if item.proposed_limit_yen is None else str(item.proposed_limit_yen)
                    ),
                    saved_terms_unavailable_reasons=[
                        reason.model_dump(mode="json") for reason in item.terms_unavailable_reasons
                    ],
                    saved_ceiling_calculation=None,
                )
            order_blockers: tuple[GateReason, ...] = ()
            sessions = None
            if item.action in {"buy", "sell"}:
                if item.action == "buy" or item.conditional_recovery is None:
                    sessions = order_sessions()
                order_blockers = terms_unavailable_reasons(
                    item,
                    inputs,
                    now=now,
                    session=None if sessions is None else sessions.confirmed_session,
                )
                if item.terms is None:
                    order_blockers += (
                        GateReason(
                            code="order_terms_unconfirmed",
                            message="保存条件は未確定です。条件を確定した新しい提案とReviewが必要です。",
                        ),
                    )
            if item.assessment_ref is not None:
                pair = verified_pairs[item.assessment_ref.assessment_id]
                assessments[pair.assessment_id] = pair.document
                current_assessment_id = latest_assessment_id(connection, item.ticker)
                latest = current_assessment_id == pair.assessment_id
                row["latest_assessment_id"] = current_assessment_id
                if item.action in {"no_action", "defer"} and not latest:
                    changes.append(
                        GateReason(
                            code="assessment_revision_changed",
                            message=(
                                f"{item.ticker} の参照評価より新しい評価があります。"
                                "保存時の行動は変更せず、見送り・保留の理由と条件を再確認してください。"
                            ),
                        )
                    )
                if item.action == "buy" and not corrected:
                    assert sessions is not None
                    calculation, blockers = _preview_buy(
                        item,
                        pair,
                        inputs,
                        latest=latest,
                        order_blockers=order_blockers,
                        sessions=sessions,
                        sqlite_path=sqlite_path,
                        now=now,
                        market_connection=market_connection,
                    )
                    row.update(calculation)
                elif item.action in {"hold", "sell"} and not corrected:
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
                elif item.action == "no_action" and latest and not corrected:
                    quote = next(
                        (quote for quote in inputs.quotes if quote.ticker == item.ticker), None
                    )
                    case = pair.document.price_value_case
                    acquisition_issue = None
                    if case is not None and case.acquisition_limit is not None:
                        bounds = order_sessions()
                        acquisition_issue = acquisition_blocker(
                            pair.document,
                            bounds.confirmed_session,
                            earliest_possible_day=bounds.earliest_possible_day,
                        )
                    if acquisition_issue is not None:
                        row["current_quote_unavailable_reason"] = acquisition_issue.model_dump(
                            mode="json"
                        )
                    else:
                        row["current_quote_calculation"] = current_price_projection(
                            pair.document,
                            price_yen=None if quote is None else quote.price_yen,
                            price_as_of=None if quote is None else quote.price_as_of,
                            as_of=now.astimezone(JST).date(),
                            basis_confirmed=assessment_quote_basis(
                                pair.document,
                                quote,
                                sqlite_path=sqlite_path,
                                now=now,
                                market_connection=market_connection,
                            ),
                            max_quote_age_days=PORTFOLIO_POLICY["valuation"][
                                "market_price_max_age_days"
                            ],
                        )
            if item.action in {"buy", "sell"}:
                blockers.extend(order_blockers)
                revalidation = row["saved_limit_revalidation"] or row.get(
                    "saved_ceiling_calculation"
                )
                if isinstance(revalidation, dict):
                    if item.action == "sell" and blockers:
                        revalidation["quantity"] = None
                    revalidation["hard_blockers"] = [
                        reason.model_dump(mode="json") for reason in blockers
                    ]
            if blockers:
                blocked = True
                changes.extend(blockers)
            row["blockers"] = [reason.model_dump(mode="json") for reason in blockers]
            rows.append(row)
        # 訂正済みの行動は適用せず、実際の保有・現金の診断だけを残す。
        warnings = build_proposal_warnings(
            inputs,
            () if corrected else proposal.items,
            proposal.liquidity_context,
            assessments=assessments,
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
            records,
            _market_assessment_context(
                current_market, saved_context_id=proposal.market_context_assessment_id, now=now
            ),
        )
