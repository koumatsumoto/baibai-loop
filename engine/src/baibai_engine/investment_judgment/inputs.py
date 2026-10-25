"""保存済み分析と文脈を調査workspaceへ投影する。取得・store書込は行わない。"""

from __future__ import annotations

from dataclasses import asdict
from datetime import date, datetime
from pathlib import Path
from typing import cast

from pydantic import JsonValue

from baibai_engine.investment_judgment.company_growth_projection import project_company_growth
from baibai_engine.investment_judgment.generated_inputs import project_generated_inputs
from baibai_engine.investment_judgment.market_context.projection import assessment_projection
from baibai_engine.investment_judgment.market_context.query import latest_assessment
from baibai_engine.investment_judgment.selection import (
    AnalysisInputSnapshot,
    GeneratedResearchSelection,
    InputGap,
    ResearchInputsSnapshot,
    ResearchSelectionRecord,
    selection_targets,
)
from baibai_engine.machine_analysis.company_growth.query import company_growth
from baibai_engine.machine_analysis.company_valuation.metrics.history import (
    VALUATION_HISTORY_SESSIONS,
)
from baibai_engine.machine_analysis.company_valuation.query import (
    balance_sheet_from_analysis,
    cash_flow_from_analysis,
    forecast_sources_from_analysis,
    machine_valuation_from_analysis,
    valuation_metrics_from_analysis,
)
from baibai_engine.machine_analysis.external_assessment.query import external_assessment
from baibai_engine.machine_analysis.market_context.reading.query import reading_snapshot
from baibai_engine.machine_analysis.market_context.security import build_security_market_context
from baibai_engine.machine_analysis.market_context.snapshot import build_market_snapshot
from baibai_engine.machine_analysis.technical_analysis.query import technical_analysis


def collect_research_inputs(
    *,
    snapshot: AnalysisInputSnapshot,
    input_gaps: tuple[InputGap, ...],
    tickers: tuple[str, ...],
    research_as_of: date,
    input_known_at: datetime,
    market_path: Path,
    indicators_path: Path,
    app_path: Path,
    company_growth_mirror: Path = Path("stores"),
    company_growth_start: date | None = None,
    company_growth_end: date | None = None,
) -> ResearchInputsSnapshot:
    context = latest_assessment(app_path, as_of=research_as_of, known_at=input_known_at)
    return ResearchInputsSnapshot(
        analysis=snapshot,
        input_gaps=input_gaps,
        market_context={
            "market": build_market_snapshot(sqlite_path=market_path, asof_date=research_as_of),
            "reading": reading_snapshot(indicators_path, asof=research_as_of),
            "saved_market_context_assessment": None
            if context is None
            else assessment_projection(context, as_of=research_as_of),
        },
        securities_context={
            ticker: {
                "company_growth": company_growth(
                    market_path,
                    ticker=ticker,
                    as_of=research_as_of,
                    start=company_growth_start,
                    end=company_growth_end,
                    mirror=company_growth_mirror,
                ).model_dump(mode="json"),
                "technical_analysis": technical_analysis(
                    market_path, ticker=ticker, as_of=research_as_of
                ),
                "market_context": build_security_market_context(
                    sqlite_path=market_path,
                    ticker=ticker,
                    asof_date=research_as_of,
                    self_range_window_bars=VALUATION_HISTORY_SESSIONS,
                ),
                "external_assessment": external_assessment(
                    market_path, ticker=ticker, cutoff=input_known_at, evaluated_at=input_known_at
                ).model_dump(mode="json"),
            }
            for ticker in tickers
        },
    )


def project_research_inputs(
    selection: ResearchSelectionRecord, *, company_growth_periods: int | None = 12
) -> dict[str, JsonValue]:
    if isinstance(selection, GeneratedResearchSelection):
        return {
            **project_generated_inputs(
                selection.inputs.generation, company_growth_periods=company_growth_periods
            ),
            "research_as_of": selection.research_as_of.isoformat(),
            "known_at": selection.input_known_at.isoformat(),
            "saved_market_context_assessment": selection.inputs.saved_market_context_assessment,
            "screening_selection": None
            if selection.inputs.screening_selection is None
            else {
                "selection_id": selection.inputs.screening_selection_id,
                "published_at": selection.inputs.screening_published_at.isoformat()
                if selection.inputs.screening_published_at is not None
                else None,
                "generation_id": selection.inputs.generation.generation_id,
                "universe_count": len(selection.inputs.screening_selection.universe_tickers),
                "result": selection.inputs.model_dump(mode="json", include={"screening_selection"})[
                    "screening_selection"
                ]["result"],
            },
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
            "cash_flow": None if analysis is None else cash_flow_from_analysis(analysis),
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
            "research_targets": [
                target.model_dump(mode="json") for target in selection_targets(selection)
            ],
            "research_selection": {
                "request": selection.request.model_dump(mode="json"),
                "scope": selection.request.scope,
                "selection_summary": selection.selection_summary,
                "targets": [target.model_dump(mode="json") for target in selection.targets],
            },
        },
    )
