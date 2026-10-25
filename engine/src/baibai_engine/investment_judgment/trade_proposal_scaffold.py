"""固定scope/入力から執筆draftを作る。未記述の比較を経済的な見送りにしない。"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from uuid import uuid4

from baibai_engine.foundation.time import JST
from baibai_engine.investment_judgment.trade_proposal import (
    DeferItem,
    GateReason,
    ProposalContentReview,
    ProposalItem,
    TradeProposal,
)
from baibai_engine.investment_judgment.trade_proposal_inputs import build_proposal_context
from baibai_engine.investment_judgment.trade_proposal_warnings import build_proposal_warnings


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
                "保存評価の各領域を踏まえ、今買う・待つ・見送る等の行動と比較理由を記述する。"
                "重要なtechnical所見との関係を示し、technicalだけでPmax・gate・sell条件を変えない。"
            )
            message = "行動・具体条件・比較理由の執筆と独立Reviewが未了です。"
            code = "proposal_content_requires_review"
        items.append(
            DeferItem(
                ticker=outcome.ticker,
                action="defer",
                assessment_ref=reference,
                rationale=rationale,
                review_conditions=(
                    "根拠・入力・見直し条件を今回の内容Reviewで確認する。",
                    (
                        "technicalを使う場合は再確認条件と時点・basis・期間の整合、"
                        "価格局面と企業価値の混同、同じ価格情報の多重票を確認する。"
                    ),
                ),
                blockers=(
                    GateReason(
                        code=code,
                        message=message,
                    ),
                ),
                resume_conditions=("不足を確認して行動と理由を記述し、独立content Reviewを行う。",),
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
            "完成した評価の価値・仮定・期間、環境伝達、外部予測との相違、重要な価格局面を踏まえ、"
            "今買う・待つ・見送る理由を現金維持等の代替案と比較する。"
            "technical欠測で他領域を一律停止せず、未完成は経済的見送りへ混ぜない。"
        ),
        forgone="採らない行動と理由を記述してください。",
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
