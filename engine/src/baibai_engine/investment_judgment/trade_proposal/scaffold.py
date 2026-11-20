"""正規contextの作業用投影。未執筆の行動や独立Reviewを生成しない。"""

from baibai_engine.investment_judgment.trade_proposal.contracts import (
    INVESTMENT_PROPOSAL_SCHEMA_VERSION,
)
from baibai_engine.investment_judgment.trade_proposal.inputs import ProposalAuthoringContext
from baibai_engine.investment_judgment.trade_proposal.warnings import build_proposal_warnings
from baibai_engine.primitives.time import JST


def scaffold_proposal(context: ProposalAuthoringContext) -> dict[str, object]:
    """固定事実と空欄だけを渡す。未完の調査理由も経済上の行動ではない。"""
    inputs = context.inputs
    baseline = build_proposal_warnings(inputs, (), context.liquidity_context, assessments={})
    return {
        "kind": "investment_proposal",
        "schema_version": INVESTMENT_PROPOSAL_SCHEMA_VERSION,
        "proposal_id": context.proposal_id,
        "as_of": inputs.checked_at.astimezone(JST).date().isoformat(),
        "prepared_at": inputs.checked_at.isoformat(),
        "request_scope": context.request_scope,
        "research_scope": context.research_scope.model_dump(mode="json"),
        "headline": None,
        "comparison": None,
        "forgone": None,
        "market_context_assessment_id": context.market_context_assessment_id,
        "inputs": inputs.model_dump(mode="json"),
        "items": [
            {
                "ticker": outcome.ticker,
                "assessment_ref": outcome.assessment_ref.model_dump(mode="json")
                if outcome.status == "reviewed"
                else None,
                "action": None,
                "rationale": None,
                "review_conditions": [],
            }
            for outcome in context.research_scope.outcomes
        ],
        "review": None,
        "liquidity_context": [row.model_dump(mode="json") for row in context.liquidity_context],
        "warnings": list(baseline.warnings),
        "portfolio_exposure": baseline.portfolio_exposure.model_dump(mode="json"),
    }
