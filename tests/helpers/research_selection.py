"""合成Research依頼・固定入力。実保有やprovider rawを含めない。"""

from datetime import datetime
from pathlib import Path
from uuid import uuid4

from baibai_engine.primitives.time import JST
from baibai_engine.investment_judgment.operation.models import OperationPayload, OperationSession
from baibai_engine.investment_judgment.selection import (
    AnalysisInputSnapshot,
    InputGap,
    ResearchInputsSnapshot,
    ResearchRequest,
    ResearchSelection,
    SelectedTarget,
)
from baibai_engine.investment_judgment.selection_policy import CURRENT_RESEARCH_CAPACITY
from baibai_engine.machine_analysis.screening.run_store import ScreeningRunReader
from tests.helpers.screening_run import screening_run_payload, seed_saved_run, stock_analysis


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
    as_of = now.astimezone(JST).date()
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
        rows = rows if rows is not None else [stock_analysis(t, name="合成会社") for t in tickers]
        payload = screening_run_payload(
            as_of=as_of.isoformat(), run_at=now.isoformat(), stock_analyses=rows
        )
        seed_saved_run(runs, payload, run_revision_id="run-synthetic")
        saved = ScreeningRunReader(runs).get_run("run-synthetic")
        assert saved is not None
        snapshot = AnalysisInputSnapshot(
            run_revision_id="run-synthetic",
            analysis_as_of=as_of,
            run_at=now,
            screening_rules_hash=payload["screening_rules_hash"],
            er_model_version=payload["er_model_version"],
            analyses={row["ticker"]: row for row in saved.stock_analyses},
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
        research_as_of=as_of,
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


def historical_operation(value):
    """既知の旧保存Selectionを持つ歴史fixture。入力収集や新規startは行わない。"""
    holding = value.request.scope == "review_positions"
    return OperationSession(
        operation_id=f"research-op-{uuid4().hex}",
        session_kind="position-review" if holding else "capital-allocation",
        status="active",
        as_of=value.research_as_of,
        ticker=value.targets[0].ticker if holding and len(value.targets) == 1 else None,
        started_at=value.input_known_at,
        payload=OperationPayload(
            checkpoint="historical saved selection", artifacts=(value.model_dump(mode="json"),)
        ),
    )
