"""保存済み分析と文脈を調査workspaceへ投影する。取得・store書込は行わない。"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict
from typing import cast

from pydantic import JsonValue

from baibai_engine.investment_judgment.cash_flow_use import cash_flow_limit, label_reported_cash
from baibai_engine.investment_judgment.workflow.company_growth_projection import (
    project_company_growth,
)
from baibai_engine.investment_judgment.workflow.generated_inputs import project_generated_inputs
from baibai_engine.investment_judgment.workflow.screening_brief import (
    technical_financial_screening_brief,
)
from baibai_engine.investment_judgment.workflow.selection import (
    GeneratedResearchSelectionV5,
    GeneratedSelection,
    GeneratedSelectionWithContext,
    ResearchSelectionRecord,
    selection_targets,
)
from baibai_engine.machine_analysis.historical_evaluation.saved_analysis import (
    balance_sheet_from_analysis,
    cash_flow_from_analysis,
    forecast_sources_from_analysis,
    machine_valuation_from_analysis,
    valuation_metrics_from_analysis,
)
from baibai_engine.machine_analysis.screening.policies.technical_financial import (
    TechnicalFinancialSelection,
)
from baibai_engine.primitives.time import JST


def project_research_inputs(
    selection: ResearchSelectionRecord, *, company_growth_periods: int | None = 12
) -> dict[str, JsonValue]:
    selection_context: dict[str, JsonValue] = {
        "research_targets": [
            target.model_dump(mode="json") for target in selection_targets(selection)
        ],
        "research_selection": {
            "request": selection.request.model_dump(mode="json"),
            "scope": selection.request.scope,
            "selection_summary": selection.selection_summary,
            "targets": [target.model_dump(mode="json") for target in selection.targets],
        },
    }
    if isinstance(selection, GeneratedSelection):
        return {
            **project_generated_inputs(
                selection.inputs.generation, company_growth_periods=company_growth_periods
            ),
            **(
                {
                    "saved_company_context": selection.inputs.model_dump(
                        mode="json", include={"saved_company_context"}
                    )["saved_company_context"]
                }
                if isinstance(selection, GeneratedSelectionWithContext)
                else {}
            ),
            "research_as_of": selection.research_as_of.isoformat(),
            "known_at": selection.input_known_at.isoformat(),
            "saved_market_context_assessment": selection.inputs.saved_market_context_assessment,
            "screening_selection": _case_screening_context(selection)
            if isinstance(selection, GeneratedResearchSelectionV5)
            else None
            if selection.inputs.screening_selection is None
            else {
                "selection_id": selection.inputs.screening_selection_id,
                "published_at": selection.inputs.screening_published_at.isoformat()
                if selection.inputs.screening_published_at is not None
                else None,
                "generation_id": selection.inputs.generation.generation_id,
                "universe_count": len(selection.inputs.screening_selection.universe_tickers),
                **(
                    {"price_inputs": deepcopy(selection.inputs.screening_selection.price_inputs)}
                    if selection.inputs.screening_selection.price_inputs is not None
                    else {}
                ),
                "result": {
                    **technical_financial_screening_brief(
                        selection.inputs.screening_selection.result,
                        page=None,
                        comparison_tickers=tuple(target.ticker for target in selection.targets),
                    ),
                    "full_result_ref": "/inputs/screening_selection/result",
                }
                if isinstance(
                    selection.inputs.screening_selection.result, TechnicalFinancialSelection
                )
                else selection.inputs.model_dump(
                    mode="json", include={"screening_selection": {"result"}}
                )["screening_selection"]["result"],
            },
            **selection_context,
        }
    snapshot = selection.inputs.analysis
    securities: dict[str, object] = {}
    for target in selection.targets:
        ticker = target.ticker
        analysis = snapshot.analyses.get(ticker)
        context = dict(selection.inputs.securities_context[ticker])
        if "company_growth" in context:
            context["company_growth"] = project_company_growth(
                context["company_growth"], ticker=ticker, periods=company_growth_periods
            )
        cash = None if analysis is None else cash_flow_from_analysis(analysis)
        limit = cash_flow_limit(
            context.get("market_context"),
            ticker=ticker,
            as_of=min(selection.research_as_of, selection.input_known_at.astimezone(JST).date()),
        )
        if cash is not None and limit is not None:
            for source, fields in (
                (
                    "edinet",
                    {
                        "fcf_yield": "reported_cfo_less_capex_to_market_cap",
                        "ttm_quality": "reported_fcf_period_quality",
                    },
                ),
                (
                    "jquants",
                    {
                        "ocf_yield": "reported_cfo_to_market_cap",
                        "ttm_quality": "reported_ocf_period_quality",
                    },
                ),
            ):
                section = cash.get(source)
                if isinstance(section, dict):
                    label_reported_cash(section, limit, fields)
        securities[ticker] = {
            **context,
            "valuation_metrics": None
            if analysis is None
            else valuation_metrics_from_analysis(analysis),
            "machine_valuation": None
            if analysis is None
            else {
                "role": (
                    "Statistical secondary prior, not company-specific value or future cash "
                    "distribution. Gross share-count changes include treasury cancellations."
                ),
                "run_revision_id": snapshot.run_revision_id,
                "as_of": None
                if snapshot.analysis_as_of is None
                else snapshot.analysis_as_of.isoformat(),
                "estimate": asdict(machine_valuation_from_analysis(analysis)),
            },
            "forecast_sources": None
            if analysis is None
            else forecast_sources_from_analysis(analysis),
            "cash_flow": cash,
            "balance_sheet": None if analysis is None else balance_sheet_from_analysis(analysis),
            **(
                {"machine_analysis_unavailable_reason": snapshot.unavailable[ticker]}
                if analysis is None
                else {}
            ),
        }
    return cast(
        dict[str, JsonValue],
        {
            "research_as_of": selection.research_as_of.isoformat(),
            "known_at": selection.input_known_at.isoformat(),
            "analysis_source": {
                "run_revision_id": snapshot.run_revision_id,
                "analysis_as_of": None
                if snapshot.analysis_as_of is None
                else snapshot.analysis_as_of.isoformat(),
                "run_at": None if snapshot.run_at is None else snapshot.run_at.isoformat(),
                "screening_rules_hash": snapshot.screening_rules_hash,
                "er_model_version": snapshot.er_model_version,
            },
            "market_context": selection.inputs.market_context,
            "securities": securities,
            **selection_context,
        },
    )


def _case_screening_context(selection: GeneratedResearchSelectionV5) -> dict[str, JsonValue] | None:
    screening = selection.inputs.screening_selection
    if screening is None:
        return None
    result = selection.inputs.model_dump(mode="json", include={"screening_selection": {"result"}})[
        "screening_selection"
    ]["result"]
    if isinstance(screening.result, TechnicalFinancialSelection):
        detail_subjects = {row.ticker for row in screening.result.rows}
        result["comparison_rows"] = result.pop("rows")
    else:
        detail_subjects = set(screening.result.tickers)
    universe = set(screening.universe_tickers)
    return {
        "selection_id": selection.inputs.screening_selection_id,
        "source_sha256": screening.source_sha256,
        "published_at": selection.inputs.screening_published_at.isoformat()
        if selection.inputs.screening_published_at
        else None,
        "generation_id": screening.generation_id,
        "universe_count": len(screening.universe_tickers),
        **(
            {"candidate_count": screening.legacy_candidate_count}
            if screening.legacy_candidate_count is not None
            else {"explorable_count_not_investment_eligibility": len(universe)}
        ),
        "population": [
            {
                "scope": item.scope,
                "technical_state": item.technical_state,
                "financial_state": item.financial_state,
                **(
                    {"models_recorded": item.models_recorded}
                    if item.models_recorded is not None
                    else {}
                ),
                "count": len(item.tickers),
            }
            for item in screening.population_groups
        ],
        "comparison_tickers": list(selection.comparison_tickers),
        "outside_published_universe": [
            t for t in selection.evidence_subjects() if t not in universe
        ],
        "no_saved_screening_detail": [
            t for t in selection.evidence_subjects() if t not in detail_subjects
        ],
        "detail_scope": "other_company_details_not_retained",
        **(
            {"price_inputs": deepcopy(screening.price_inputs)}
            if screening.price_inputs is not None
            else {}
        ),
        "result": result,
    }
