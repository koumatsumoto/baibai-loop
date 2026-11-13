"""保存本文とexact pairだけを投影する。現在条件の再評価は行わない。"""

from __future__ import annotations

import sqlite3
from collections.abc import Mapping
from datetime import datetime

from baibai_engine.investment_judgment.stock_assessment.scenario import project_return
from baibai_engine.investment_judgment.stock_assessment.store import ReviewedAssessment
from baibai_engine.investment_judgment.trade_proposal.contracts import (
    ExposureRow,
    GeneratedLiquidityContext,
    LimitTerms,
    ProposalItem,
    TradeProposal,
)
from baibai_engine.primitives.numbers import finite_decimal
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
    SavedModelPerspectiveView,
    SavedNoActionItemView,
    SavedPerspectiveSourceView,
    SavedPortfolioExposureView,
    SavedProposalItemView,
    SavedProposalView,
    SavedQuoteView,
    SavedReasonView,
    SavedRemainingRewardView,
    SavedResolvedValuationView,
    SavedSellItemView,
    SavedTurnoverWindowView,
    SavedUnresolvedValuationView,
    SavedValuationProjectionView,
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
    perspective_sources = {
        source_id for item in document.model_perspectives for source_id in item.source_ids
    }
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
        model_perspectives=[
            SavedModelPerspectiveView.model_validate(item.model_dump(mode="json"))
            for item in document.model_perspectives
        ],
        model_perspective_sources=[
            SavedPerspectiveSourceView.model_validate(source.model_dump(mode="json"))
            for source in document.input_snapshot.sources
            if source.source_id in perspective_sources
        ],
        valuation=_valuation(pair),
        review=SavedAssessmentReviewView(
            reviewed_at=review.reviewed_at,
            primary_source_check=review.primary_source_check,
            review_scope=review.review_scope,
            strongest_countercase=review.strongest_countercase,
            uncertainty_acceptance_reason=review.uncertainty_acceptance_reason,
        ),
    )


def _valuation(
    pair: ReviewedAssessment,
) -> SavedResolvedValuationView | SavedUnresolvedValuationView:
    valuation = pair.document.valuation
    if valuation.status == "unresolved":
        assert valuation.unresolved_reason is not None
        return SavedUnresolvedValuationView(reason=valuation.unresolved_reason)
    assert valuation.horizon_months is not None
    assert valuation.required_annual_return_pct is not None
    assert valuation.base is not None
    assert valuation.downside is not None
    price_fact = next(
        fact
        for fact in pair.document.input_snapshot.facts
        if fact.fact_id == valuation.market_price_fact_id
    )
    price = finite_decimal(price_fact.value)
    projections = []
    for projection in (valuation.base, valuation.downside):
        # Use the Assessment's entry price and horizon, not the Proposal quote/limit or today.
        returns = project_return(
            projection, price_yen=price, horizon_months=valuation.horizon_months
        )
        projections.append(
            SavedValuationProjectionView(
                terminal_value_per_share_yen=str(projection.terminal_value_per_share_yen),
                cash_distribution_per_share_yen=str(projection.cash_distribution_per_share_yen),
                annualized_return_pct=str(round(returns.annualized_return_pct, 4)),
                calculation=projection.calculation,
            )
        )
    return SavedResolvedValuationView(
        price_yen=str(price),
        price_as_of=price_fact.as_of,
        price_observed_at=price_fact.observed_at,
        price_basis=price_fact.price_basis,
        horizon_months=valuation.horizon_months,
        required_annual_return_pct=str(valuation.required_annual_return_pct),
        base=projections[0],
        downside=projections[1],
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
                price_window_end=(
                    liquidity.price_window_end
                    if isinstance(liquidity, GeneratedLiquidityContext)
                    else None
                ),
                adv_yen=None if liquidity.adv_yen is None else str(liquidity.adv_yen),
                turnover_window=(
                    SavedTurnoverWindowView.model_validate(
                        liquidity.turnover_window.model_dump(mode="json")
                    )
                    if isinstance(liquidity, GeneratedLiquidityContext)
                    and liquidity.turnover_window is not None
                    else None
                ),
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
    if item.action == "hold" or item.action == "sell":
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
