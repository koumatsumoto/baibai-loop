"""固定scope/入力から執筆draftを作る。未記述の比較を経済的な見送りにしない。"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from uuid import uuid4

from baibai_engine.primitives.time import JST
from baibai_engine.investment_judgment.trade_proposal.contracts import (
    DeferItem,
    GateReason,
    ProposalContentReview,
    ProposalItem,
    TradeProposal,
)
from baibai_engine.investment_judgment.trade_proposal.inputs import build_proposal_context
from baibai_engine.investment_judgment.trade_proposal.warnings import build_proposal_warnings


def scaffold_proposal(
    *,
    operation_id: str,
    proposal_id: str,
    db_path: Path | None,
    sqlite_path: Path,
    now: datetime,
) -> dict[str, object]:
    context = build_proposal_context(
        operation_id=operation_id,
        proposal_id=proposal_id,
        db_path=db_path,
        sqlite_path=sqlite_path,
        now=now,
    )
    items: list[ProposalItem] = []
    for outcome in context.research_scope.outcomes:
        if outcome.status == "unfinished":
            reference = None
            rationale = message = outcome.unfinished_reason
            code = "research_unfinished"
        else:
            reference = outcome.assessment_ref
            rationale = (
                "保存した銘柄評価を踏まえ、今買う・待つ・見送る等の行動と比較理由を記述する。"
                "重要なモデル観測・一次資料と仮定の関係を示し、"
                "価格局面だけでPmax・購入条件・売却条件を変えない。"
            )
            message = "行動・具体条件・比較理由の執筆と独立検証が未了です。"
            code = "proposal_content_requires_review"
        items.append(
            DeferItem(
                ticker=outcome.ticker,
                action="defer",
                assessment_ref=reference,
                rationale=rationale,
                review_conditions=(
                    "根拠・入力と見直す時点を確認し、買付では未約定・部分約定・新情報の後に何を再確認するかを記す。",
                    (
                        "採否を変える材料について時点・basis・期間と再確認条件を揃え、"
                        "同じ原情報の重複加点を避ける。全シグナルのコメントは要求しない。"
                    ),
                ),
                blockers=(
                    GateReason(
                        code=code,
                        message=message,
                    ),
                ),
                resume_conditions=("不足を確認して行動と理由を記述し、独立検証を行う。",),
            )
        )
    warnings = build_proposal_warnings(
        context.inputs,
        tuple(items),
        context.liquidity_context,
        assessments={identity: pair.document for identity, pair in context.assessments.items()},
    )
    proposal = TradeProposal(
        proposal_id=proposal_id,
        as_of=now.astimezone(JST).date(),
        prepared_at=now,
        published_at=now,
        request_scope=context.request_scope,
        research_scope=context.research_scope,
        headline="提案の要点を記述してください。",
        comparison=(
            "完成した評価を現金維持等と比べ、選択理由と反転条件を記す。新規買付は"
            "docs/reference/trade-proposal.md#limit-selectionに従い、"
            "limit候補の見返り・数量・cashと待機案を比較する。未完成を経済的見送りへ混ぜない。"
        ),
        forgone="採らない行動と理由（新規買付では別の指値・待機案）を記述してください。",
        market_context_assessment_id=context.market_context_assessment_id,
        inputs=context.inputs,
        items=tuple(items),
        review=ProposalContentReview(
            review_id=f"proposal-review-{uuid4()}",
            attempt=1,
            reviewer_identity="独立Reviewerのidentityを記述してください。",
            reviewed_at=now,
            conclusion="pass",
            draft_sha256="0" * 64,
        ),
        liquidity_context=context.liquidity_context,
        warnings=warnings.warnings,
        portfolio_exposure=warnings.portfolio_exposure,
    )
    return proposal.payload()
