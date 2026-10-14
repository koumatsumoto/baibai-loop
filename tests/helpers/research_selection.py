"""合成Research依頼・固定入力。実保有やprovider rawを含めない。"""

from datetime import datetime
from pathlib import Path

from baibai_engine.research.selection import (
    AnalysisInputSnapshot,
    InputGap,
    ResearchInputsSnapshot,
    ResearchRequest,
    ResearchSelection,
    ResearchSelectionDraft,
    SelectedTarget,
)
from baibai_engine.research.selection_policy import CURRENT_RESEARCH_CAPACITY
from baibai_engine.screening.run_store import ScreeningRunStore
from tests.helpers.screening_run import screening_run_payload, security_analysis


def selected_target(ticker="1234", purpose="new"):
    return SelectedTarget(
        ticker=ticker,
        purpose=purpose,
        rationale="合成例：根拠を比較した",
        research_question="正常利益を確認できるか",
        key_risk="一時利益の誤認",
    )


def selection(
    *, now: datetime, tickers=(), scope="discover_new", runs: Path | None = None, rows=None
):
    targets = tuple(
        selected_target(t, "holding" if scope == "review_positions" else "new") for t in tickers
    )
    gaps = (
        ()
        if runs
        else tuple(
            InputGap(ticker=t, reason="合成機械入力なし", attempted_sources=("synthetic source",))
            for t in tickers
        )
    )
    if runs:
        rows = (
            rows if rows is not None else [security_analysis(t, name="合成会社") for t in tickers]
        )
        payload = screening_run_payload(
            as_of=now.date().isoformat(), run_at=now.isoformat(), security_analyses=rows
        )
        ScreeningRunStore(runs).publish_run(payload, run_revision_id="run-synthetic")
        snapshot = AnalysisInputSnapshot(
            run_revision_id="run-synthetic",
            analysis_as_of=now.date(),
            run_at=now,
            screening_rules_hash=payload["screening_rules_hash"],
            er_model_version=payload["er_model_version"],
            analyses={row["ticker"]: row for row in rows},
        )
    else:
        snapshot = AnalysisInputSnapshot(
            run_revision_id=None,
            analysis_as_of=None,
            run_at=None,
            screening_rules_hash=None,
            er_model_version=None,
            analyses={},
            unavailable={gap.ticker: gap.reason for gap in gaps},
        )
    return ResearchSelection(
        request=ResearchRequest(
            text="合成例：投資提案して",
            scope=scope,
            requested_tickers=tickers if scope == "specified_stocks" else (),
        ),
        research_as_of=now.date(),
        input_known_at=now,
        selection_summary="合成例：今回の対象と未調査範囲を示す",
        targets=targets,
        capacity=CURRENT_RESEARCH_CAPACITY,
        inputs=ResearchInputsSnapshot(
            analysis=snapshot,
            market_context={
                "market": None,
                "reading": None,
                "saved_market_context_assessment": None,
            },
            securities_context={
                t: {"market_context": None, "external_assessment": None} for t in tickers
            },
            input_gaps=gaps,
        ),
    )


def draft_for(value):
    return ResearchSelectionDraft(
        schema_version=1,
        request=value.request,
        research_as_of=value.research_as_of,
        input_known_at=value.input_known_at,
        run_revision_id=value.inputs.analysis.run_revision_id,
        selection_summary=value.selection_summary,
        targets=value.targets,
        input_gaps=value.inputs.input_gaps,
    )
