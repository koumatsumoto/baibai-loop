"""保存済み分析と文脈を調査workspaceへ投影する。取得・store書込は行わない。"""

from __future__ import annotations

from dataclasses import asdict
from datetime import date, datetime
from pathlib import Path
from typing import cast

from pydantic import JsonValue

from baibai_engine.external_assessment.query import external_assessment
from baibai_engine.market_context.reading.query import reading_snapshot
from baibai_engine.market_context.security import build_security_market_context
from baibai_engine.market_context.snapshot import build_market_snapshot
from baibai_engine.market_context.technical_query import technical_analysis
from baibai_engine.research.market_context.projection import assessment_projection
from baibai_engine.research.market_context.query import latest_assessment
from baibai_engine.research.selection import (
    AnalysisInputSnapshot,
    InputGap,
    ResearchInputsSnapshot,
    ResearchSelection,
    selection_targets,
)
from baibai_engine.valuation.metrics.history import VALUATION_HISTORY_SESSIONS
from baibai_engine.valuation.query import (
    balance_sheet_from_analysis,
    cash_flow_from_analysis,
    machine_valuation_from_analysis,
)


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


def project_research_inputs(selection: ResearchSelection) -> dict[str, JsonValue]:
    snapshot = selection.inputs.analysis
    securities: dict[str, object] = {}
    for target in selection.targets:
        ticker = target.ticker
        analysis = snapshot.analyses.get(ticker)
        securities[ticker] = {
            **selection.inputs.securities_context[ticker],
            "machine_valuation": None
            if analysis is None
            else {
                "run_revision_id": snapshot.run_revision_id,
                "as_of": None
                if snapshot.analysis_as_of is None
                else snapshot.analysis_as_of.isoformat(),
                "estimate": asdict(machine_valuation_from_analysis(analysis)),
            },
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
