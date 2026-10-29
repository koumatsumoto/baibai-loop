"""保存本文とexact pairだけを投影する。現在条件の再評価は行わない。"""

from __future__ import annotations

import sqlite3
from collections.abc import Mapping
from datetime import datetime

from baibai_engine.investment_judgment.stock_assessment.store import ReviewedAssessment
from baibai_engine.investment_judgment.trade_proposal.contracts import (
    ExposureRow,
    LimitTerms,
    ProposalItem,
    TradeProposal,
)
from baibai_engine.primitives.time import JST
from baibai_web.readmodel.proposal_models import (
    LatestProposalView,
    SavedAssessmentReviewView,
    SavedAssessmentView,
    SavedBuyItemView,
    SavedDeferItemView,
    SavedExposureRowView,
    SavedHoldItemView,
    SavedLimitTermsView,
    SavedLiquidityView,
    SavedNoActionItemView,
    SavedPortfolioExposureView,
    SavedProposalItemView,
    SavedProposalView,
    SavedQuoteView,
    SavedReasonView,
    SavedRemainingRewardView,
    SavedSellItemView,
    UnfinishedResearchView,
)
from baibai_web.sources.protocols import ProposalSource


def build_latest_proposal(
    source: ProposalSource, *, generated_at: datetime | None = None
) -> LatestProposalView:
    now = generated_at if generated_at is not None else datetime.now(JST)
    try:
        proposal = source.latest()
    except (OSError, ValueError, RuntimeError, sqlite3.Error):
        # A damaged or missing store is not an empty publication set. Do not
        # expose paths, SQL or saved payload fragments from reader exceptions.
        return LatestProposalView(
            generated_at=now,
            status="unavailable",
            error="保存済み提案を読み取れません。",
            proposal=None,
        )
    return LatestProposalView(
        generated_at=now,
        status="empty" if proposal is None else "ready",
        error=None,
        proposal=proposal,
    )


def project_saved_proposal(
    proposal: TradeProposal, pairs: Mapping[str, ReviewedAssessment]
) -> SavedProposalView:
    """Caller supplies the exact current/original pairs from one saved read transaction."""

    exposure = proposal.portfolio_exposure
    return SavedProposalView(
        proposal_id=proposal.proposal_id,
        as_of=proposal.as_of,
        prepared_at=proposal.prepared_at,
        published_at=proposal.published_at,
        request_scope=proposal.request_scope,
        headline=proposal.headline,
        comparison=proposal.comparison,
        forgone=proposal.forgone,
        warnings=list(proposal.warnings),
        items=[_item(item, proposal, pairs) for item in proposal.items],
        portfolio_exposure=SavedPortfolioExposureView(
            total_capital_yen=(
                None if exposure.total_capital_yen is None else str(exposure.total_capital_yen)
            ),
            ticker=None if exposure.ticker is None else _exposure(exposure.ticker),
            sector=None if exposure.sector is None else _exposure(exposure.sector),
            common_factors=[_exposure(row) for row in exposure.common_factors],
            common_factor_unclassified_tickers=list(exposure.common_factor_unclassified_tickers),
        ),
    )


def _exposure(row: ExposureRow) -> SavedExposureRowView:
    return SavedExposureRowView(
        key=row.key, prospective_pct=str(row.prospective_pct), warning_pct=str(row.warning_pct)
    )


def _assessment(pair: ReviewedAssessment) -> SavedAssessmentView:
    document, review = pair.document, pair.review
    case = document.investment_case
    return SavedAssessmentView(
        assessment_id=pair.assessment_id,
        core_sha256=pair.core_sha256,
        review_id=review.review_id,
        as_of=document.input_snapshot.as_of,
        stance=document.conclusion.stance,
        investment_case_explanation=case.explanation,
        investment_case_status=case.status,
        investment_case_status_reason=case.status_reason,
        invalidation_conditions=list(case.invalidation_conditions),
        review=SavedAssessmentReviewView(
            reviewed_at=review.reviewed_at,
            primary_source_check=review.primary_source_check,
            review_scope=review.review_scope,
            strongest_countercase=review.strongest_countercase,
            uncertainty_acceptance_reason=review.uncertainty_acceptance_reason,
        ),
    )


def _terms(terms: LimitTerms | None) -> SavedLimitTermsView | None:
    if terms is None:
        return None
    return SavedLimitTermsView(
        limit_price_yen=str(terms.limit_price_yen),
        quantity=terms.quantity,
        notional_yen=str(terms.notional_yen),
        target_session=terms.target_session,
        valuation_as_of=terms.valuation_as_of,
        maximum_entry_price_yen=(
            None if terms.maximum_entry_price_yen is None else str(terms.maximum_entry_price_yen)
        ),
        conditions=list(terms.conditions),
        warnings=list(terms.warnings),
    )


def _item(
    item: ProposalItem, proposal: TradeProposal, pairs: Mapping[str, ReviewedAssessment]
) -> SavedProposalItemView:
    quote = next((row for row in proposal.inputs.quotes if row.ticker == item.ticker), None)
    outcome = next(row for row in proposal.research_scope.outcomes if row.ticker == item.ticker)
    liquidity = next((row for row in proposal.liquidity_context if row.ticker == item.ticker), None)
    common: dict[str, object] = {
        "action": item.action,
        "ticker": item.ticker,
        "rationale": item.rationale,
        "review_conditions": list(item.review_conditions),
        "assessment": (
            None
            if item.assessment_ref is None
            else _assessment(pairs[item.assessment_ref.assessment_id])
        ),
        "saved_quote": (
            None
            if quote is None
            else SavedQuoteView(
                price_yen=str(quote.price_yen),
                price_as_of=quote.price_as_of,
                price_basis=quote.price_basis,
            )
        ),
        "unfinished": (
            UnfinishedResearchView(
                reason=outcome.unfinished_reason, references=list(outcome.references)
            )
            if outcome.status == "unfinished"
            else None
        ),
        "liquidity": (
            None
            if liquidity is None
            else SavedLiquidityView(
                analysis_as_of=liquidity.analysis_as_of,
                adv_yen=None if liquidity.adv_yen is None else str(liquidity.adv_yen),
                unavailable_reason=liquidity.unavailable_reason,
            )
        ),
    }
    if item.action in {"buy", "sell"}:
        common.update(
            terms=_terms(item.terms),
            terms_unavailable_reasons=[
                SavedReasonView(code=reason.code, message=reason.message)
                for reason in item.terms_unavailable_reasons
            ],
        )
    if item.action == "buy":
        return SavedBuyItemView.model_validate(
            {
                **common,
                "timing": item.timing,
                "proposed_limit_yen": (
                    None if item.proposed_limit_yen is None else str(item.proposed_limit_yen)
                ),
            }
        )
    if item.action in {"hold", "sell"}:
        position = item.position
        original = position.original_assessment_ref
        common.update(
            original_assessment=(
                None if original is None else _assessment(pairs[original.assessment_id])
            ),
            original_investment_reason=position.original_investment_reason,
            original_reason_unavailable_reason=position.original_reason_unavailable_reason,
            case_reassessment_reason=item.case_reassessment_reason,
            remaining_reward=(
                None
                if item.remaining_reward is None
                else SavedRemainingRewardView(
                    status=item.remaining_reward.status, reason=item.remaining_reward.reason
                )
            ),
        )
        if item.action == "hold":
            return SavedHoldItemView.model_validate(
                {
                    **common,
                    "holding_basis": item.holding_basis,
                    "return_calculation_unavailable_reason": (
                        item.return_calculation_unavailable_reason
                    ),
                }
            )
        return SavedSellItemView.model_validate({**common, "reason_code": item.reason_code})
    if item.action == "no_action":
        return SavedNoActionItemView.model_validate(common)
    return SavedDeferItemView.model_validate(
        {
            **common,
            "blockers": [
                SavedReasonView(code=reason.code, message=reason.message)
                for reason in item.blockers
            ],
            "resume_conditions": list(item.resume_conditions),
        }
    )
