"""保存本文とexact pairだけを投影する。現在条件の再評価は行わない。"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from datetime import date
from decimal import Decimal
from functools import cache

from baibai_engine.investment_judgment.stock_assessment.scenario import (
    FIXED_VALUE_DELAY_MONTHS,
    Projection,
    ReturnProjection,
    project_buy_capital,
    project_return,
    valuation_conditions,
)
from baibai_engine.investment_judgment.stock_assessment.store import ReviewedAssessment
from baibai_engine.investment_judgment.trade_proposal.contracts import (
    ExposureRow,
    GeneratedLiquidityContext,
    LimitTerms,
    PositionInput,
    ProposalItem,
    TradeProposal,
)
from baibai_engine.primitives.numbers import finite_decimal
from baibai_web.readmodel.proposal_models import (
    SavedAssessmentRefreshView,
    SavedAssessmentRefView,
    SavedAssessmentReviewView,
    SavedAssessmentRisksView,
    SavedAssessmentSourceView,
    SavedAssessmentView,
    SavedBuyCapitalComparisonView,
    SavedBuyItemView,
    SavedBuyTimeSensitivityView,
    SavedCashView,
    SavedConditionalReturnView,
    SavedDeferItemView,
    SavedExposureRowView,
    SavedHoldingOriginView,
    SavedHoldingPositionView,
    SavedHoldItemView,
    SavedHoldQuoteComparisonView,
    SavedHypothesisCheckView,
    SavedLimitTermsView,
    SavedLiquidityView,
    SavedModelPerspectiveView,
    SavedNoActionItemView,
    SavedPermanentLossRiskView,
    SavedPortfolioExposureView,
    SavedPriceValueView,
    SavedProposalItemView,
    SavedProposalView,
    SavedQuoteView,
    SavedReasonView,
    SavedRemainingRewardView,
    SavedResolvedValuationView,
    SavedScenarioTimeSensitivityView,
    SavedSellItemView,
    SavedSellLimitComparisonView,
    SavedTurnoverWindowView,
    SavedUnresolvedValuationView,
    SavedValuationProjectionView,
    SavedValueGapView,
    SavedValueRealizationView,
    UnfinishedResearchView,
)

_SourceViews = Callable[[Iterable[str]], list[SavedAssessmentSourceView]]


def _holding_origin(
    position: PositionInput,
    pairs: Mapping[str, ReviewedAssessment],
    assessment: Callable[[str], SavedAssessmentView],
) -> SavedHoldingOriginView:
    reference = position.original_assessment_ref
    pair = None if reference is None else pairs.get(reference.assessment_id)
    # Pure projection leaves absent/non-exact supplied pairs unfilled. DB sources
    # validate stored references before this boundary; no newer pair is borrowed.
    exact = (
        reference is not None
        and pair is not None
        and pair.assessment_id == reference.assessment_id
        and pair.core_sha256 == reference.core_sha256
        and pair.review.review_id == reference.review_id
    )
    return SavedHoldingOriginView(
        assessment_ref=(
            None
            if reference is None
            else SavedAssessmentRefView.model_validate(reference, from_attributes=True)
        ),
        assessment=assessment(pair.assessment_id) if exact and pair is not None else None,
        investment_reason=position.original_investment_reason,
        reason_unavailable=position.original_reason_unavailable_reason,
    )


def project_saved_proposal(
    proposal: TradeProposal, pairs: Mapping[str, ReviewedAssessment]
) -> SavedProposalView:
    """Project each original Assessment/Review pair once for this saved publication."""

    @cache
    def assessment(assessment_id: str) -> SavedAssessmentView:
        return _assessment(pairs[assessment_id])

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
        saved_cash=SavedCashView(
            available_cash_yen=str(proposal.inputs.available_cash_yen),
            inputs_checked_at=proposal.inputs.checked_at,
            ledger_as_of=proposal.inputs.ledger_as_of,
        ),
        items=[_item(item, proposal, pairs, assessment) for item in proposal.items],
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


def _conditional_return(value: ReturnProjection) -> SavedConditionalReturnView:
    return SavedConditionalReturnView(
        total_value_per_share_yen=str(value.total_value_yen),
        total_return_pct=str(round(value.total_return_pct, 4)),
        annualized_return_pct=str(round(value.annualized_return_pct, 4)),
    )


def _remaining_returns(
    pair: ReviewedAssessment | None, *, as_of: date, price_yen: Decimal
) -> tuple[int, SavedConditionalReturnView, SavedConditionalReturnView] | None:
    if pair is None:
        return None
    document = pair.document
    valuation = document.valuation
    if (
        document.investment_case.status != "intact"
        or valuation.status != "resolved"
        or document.input_snapshot.as_of != as_of
    ):
        return None
    assert valuation.base is not None
    assert valuation.downside is not None
    horizon = valuation.horizon_months
    assert horizon is not None
    base, downside = (
        _conditional_return(project_return(projection, price_yen=price_yen, horizon_months=horizon))
        for projection in (valuation.base, valuation.downside)
    )
    return horizon, base, downside


def _buy_time_sensitivity(
    base: Projection,
    downside: Projection,
    *,
    price_yen: Decimal,
    horizon_months: int,
    required_annual_return_pct: Decimal,
) -> SavedBuyTimeSensitivityView:
    cases = []
    for projection in (base, downside):
        value = valuation_conditions(
            projection,
            price_yen=price_yen,
            horizon_months=horizon_months,
            required_annual_return_pct=required_annual_return_pct,
        )
        cases.append(
            SavedScenarioTimeSensitivityView(
                annualized_return_pct=str(round(value.returns.annualized_return_pct, 4)),
                delayed_annualized_return_pct=str(
                    round(value.delayed_returns.annualized_return_pct, 4)
                ),
            )
        )
    return SavedBuyTimeSensitivityView(
        assumption="terminal_and_cumulative_cash_unchanged",
        additional_months=FIXED_VALUE_DELAY_MONTHS,
        horizon_months=horizon_months + FIXED_VALUE_DELAY_MONTHS,
        base=cases[0],
        downside=cases[1],
    )


def _exposure(row: ExposureRow) -> SavedExposureRowView:
    return SavedExposureRowView(
        key=row.key, prospective_pct=str(row.prospective_pct), warning_pct=str(row.warning_pct)
    )


def _saved_price_value(
    pair: ReviewedAssessment, sources: _SourceViews
) -> SavedPriceValueView | None:
    finding = pair.recorded_price_value()
    if finding is None:
        return None
    document = pair.document
    case = document.price_value_case
    assert case is not None
    price_id = document.valuation.market_price_fact_id
    used_sources: set[str] = set()
    evidence = []
    for fact in document.input_snapshot.facts:
        if fact.fact_id in case.value_fact_ids:
            evidence.append(
                {
                    **fact.model_dump(include={"fact_id", "unit", "as_of", "source_ids"}),
                    "value": str(fact.value),
                }
            )
        elif fact.fact_id != price_id:
            continue
        used_sources.update(fact.source_ids)
    return SavedPriceValueView.model_validate(
        {**finding, "value_facts": evidence, "sources": sources(used_sources)}
    )


def _assessment(pair: ReviewedAssessment) -> SavedAssessmentView:
    document, review = pair.document, pair.review
    case = document.investment_case
    original_sources = document.input_snapshot.sources
    positions = {source.source_id: index for index, source in enumerate(original_sources)}

    @cache
    def source_view(index: int) -> SavedAssessmentSourceView:
        return SavedAssessmentSourceView.model_validate(
            original_sources[index], from_attributes=True
        )

    def sources(source_ids: Iterable[str]) -> list[SavedAssessmentSourceView]:
        indexes = sorted(positions[s] for s in positions.keys() & set(source_ids))
        return [source_view(index) for index in indexes]

    return SavedAssessmentView(
        assessment_id=pair.assessment_id,
        core_sha256=pair.core_sha256,
        review_id=review.review_id,
        as_of=document.input_snapshot.as_of,
        stance=document.conclusion.stance,
        price_value_case=_saved_price_value(pair, sources),
        investment_case_explanation=case.explanation,
        investment_case_status=case.status,
        investment_case_status_reason=case.status_reason,
        invalidation_conditions=list(case.invalidation_conditions),
        investment_case_source_ids=list(case.source_ids),
        investment_case_sources=sources(case.source_ids),
        hypothesis_checks=[
            SavedHypothesisCheckView.model_validate(item, from_attributes=True)
            for item in document.findings.hypotheses
        ],
        hypothesis_sources=sources(
            source_id for item in document.findings.hypotheses for source_id in item.source_ids
        ),
        refresh=(
            None
            if document.refresh is None
            else SavedAssessmentRefreshView.model_validate(document.refresh, from_attributes=True)
        ),
        strongest_countercase=document.conclusion.strongest_countercase,
        realization=SavedValueRealizationView.model_validate(
            document.findings.realization, from_attributes=True
        ),
        value_gap=SavedValueGapView(
            explanation=document.findings.value_gap_explanation,
            findings_source_ids=list(document.findings.source_ids),
            findings_sources=sources(document.findings.source_ids),
        ),
        model_perspectives=[
            SavedModelPerspectiveView.model_validate(item, from_attributes=True)
            for item in document.model_perspectives
        ],
        model_perspective_sources=sources(
            source_id for item in document.model_perspectives for source_id in item.source_ids
        ),
        risk_analysis=SavedAssessmentRisksView(
            risks=[
                SavedPermanentLossRiskView.model_validate(risk, from_attributes=True)
                for risk in document.permanent_loss_risks
            ],
            unresolved_questions=list(document.findings.unresolved_questions),
            sources=sources(
                source_id for item in document.permanent_loss_risks for source_id in item.source_ids
            ),
        ),
        valuation=_valuation(pair, sources),
        review=SavedAssessmentReviewView(
            reviewed_at=review.reviewed_at,
            primary_source_check=review.primary_source_check,
            review_scope=review.review_scope,
            strongest_countercase=review.strongest_countercase,
            uncertainty_acceptance_reason=review.uncertainty_acceptance_reason,
            checked_source_ids=list(review.checked_source_ids),
            checked_sources=sources(review.checked_source_ids),
        ),
    )


def _valuation(
    pair: ReviewedAssessment, sources: _SourceViews
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
                source_ids=list(projection.source_ids),
                sources=sources(projection.source_ids),
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


def _saved_position(position: PositionInput, proposal: TradeProposal) -> SavedHoldingPositionView:
    return SavedHoldingPositionView(
        quantity=position.quantity,
        quantity_basis_confirmed=position.quantity_basis_confirmed,
        cost_yen=str(position.cost_yen),
        inputs_checked_at=proposal.inputs.checked_at,
        ledger_as_of=proposal.inputs.ledger_as_of,
    )


def _item(
    item: ProposalItem,
    proposal: TradeProposal,
    pairs: Mapping[str, ReviewedAssessment],
    assessment: Callable[[str], SavedAssessmentView],
) -> SavedProposalItemView:
    quote = next((row for row in proposal.inputs.quotes if row.ticker == item.ticker), None)
    outcome = next(row for row in proposal.research_scope.outcomes if row.ticker == item.ticker)
    liquidity = next((row for row in proposal.liquidity_context if row.ticker == item.ticker), None)
    current = None if item.assessment_ref is None else pairs[item.assessment_ref.assessment_id]
    snapshot = None if current is None else current.document.input_snapshot
    common: dict[str, object] = {
        "action": item.action,
        "ticker": item.ticker,
        "company_name": None if snapshot is None else snapshot.company_name,
        "sector": None if snapshot is None else snapshot.sector,
        "rationale": item.rationale,
        "review_conditions": list(item.review_conditions),
        "assessment": None if current is None else assessment(current.assessment_id),
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
        comparison = None
        if (
            current is not None
            and item.terms is not None
            and current.document.valuation.status == "resolved"
            and current.document.input_snapshot.as_of == proposal.as_of
        ):
            valuation = current.document.valuation
            assert valuation.base is not None
            assert valuation.downside is not None
            assert valuation.horizon_months is not None
            comparison = SavedBuyCapitalComparisonView.model_validate(
                project_buy_capital(
                    valuation.base,
                    valuation.downside,
                    horizon_months=valuation.horizon_months,
                    limit=item.terms.limit_price_yen,
                    cash=proposal.inputs.available_cash_yen,
                    quantity=item.terms.quantity,
                    author_selected=True,
                )
            )
            assert valuation.required_annual_return_pct is not None
            comparison.time_sensitivity = _buy_time_sensitivity(
                valuation.base,
                valuation.downside,
                price_yen=item.terms.limit_price_yen,
                horizon_months=valuation.horizon_months,
                required_annual_return_pct=valuation.required_annual_return_pct,
            )
        return SavedBuyItemView.model_validate(
            {
                **common,
                "timing": item.timing,
                "proposed_limit_yen": (
                    None if item.proposed_limit_yen is None else str(item.proposed_limit_yen)
                ),
                "saved_buy_comparison": comparison,
            }
        )
    if item.action == "hold" or item.action == "sell":
        position = item.position
        original = position.original_assessment_ref
        common.update(
            saved_position=_saved_position(position, proposal),
            original_assessment=(None if original is None else assessment(original.assessment_id)),
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
            hold_comparison = None
            # An intact case alone does not confirm the share basis or the reward at this quote.
            if (
                item.holding_basis == "sufficient_remaining_reward"
                and item.return_calculation_unavailable_reason is None
                and position.quantity_basis_confirmed
                and quote is not None
                and (
                    returns := _remaining_returns(
                        current, as_of=proposal.as_of, price_yen=quote.price_yen
                    )
                )
                is not None
            ):
                horizon, base, downside = returns
                hold_comparison = SavedHoldQuoteComparisonView(
                    calculation_price_yen=str(quote.price_yen),
                    price_as_of=quote.price_as_of,
                    price_basis=quote.price_basis,
                    horizon_months=horizon,
                    base=base,
                    downside=downside,
                )
            return SavedHoldItemView.model_validate(
                {
                    **common,
                    "holding_basis": item.holding_basis,
                    "saved_hold_quote_comparison": hold_comparison,
                    "return_calculation_unavailable_reason": (
                        item.return_calculation_unavailable_reason
                    ),
                }
            )
        sell_comparison = None
        if (
            item.reason_code == "insufficient_remaining_reward"
            and item.terms is not None
            and (
                returns := _remaining_returns(
                    current, as_of=proposal.as_of, price_yen=item.terms.limit_price_yen
                )
            )
            is not None
        ):
            horizon, base, downside = returns
            sell_comparison = SavedSellLimitComparisonView(
                calculation_price_yen=str(item.terms.limit_price_yen),
                horizon_months=horizon,
                base=base,
                downside=downside,
            )
        return SavedSellItemView.model_validate(
            {
                **common,
                "reason_code": item.reason_code,
                "saved_sell_limit_comparison": sell_comparison,
            }
        )
    if item.action == "no_action":
        return SavedNoActionItemView.model_validate(common)
    deferred_position = next(
        (row for row in proposal.inputs.positions if row.ticker == item.ticker), None
    )
    return SavedDeferItemView.model_validate(
        {
            **common,
            "saved_position": (
                _saved_position(deferred_position, proposal)
                if outcome.purpose == "holding" and deferred_position is not None
                else None
            ),
            "saved_holding_origin": (
                _holding_origin(deferred_position, pairs, assessment)
                if outcome.purpose == "holding" and deferred_position is not None
                else None
            ),
            "blockers": [
                SavedReasonView(code=reason.code, message=reason.message)
                for reason in item.blockers
            ],
            "resume_conditions": list(item.resume_conditions),
        }
    )
