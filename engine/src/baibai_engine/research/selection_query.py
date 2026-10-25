"""保存済み入力の選定contextと初回照合。取得・書込・AI起動は行わない。"""

from __future__ import annotations

from contextlib import closing
from datetime import date, datetime
from pathlib import Path
from typing import Any

from baibai_engine.collection.identifiers import normalize_ticker
from baibai_engine.foundation.time import JST
from baibai_engine.position.ledger import replay_events_through
from baibai_engine.position.ledger_read import load_ledger_document
from baibai_engine.research.market_context.projection import assessment_projection
from baibai_engine.research.market_context.query import latest_assessment
from baibai_engine.research.selection import AnalysisInputSnapshot, InputGap, ResearchRequest
from baibai_engine.research.selection_policy import CURRENT_RESEARCH_CAPACITY
from baibai_engine.screening.discovery.projection import candidate_rows
from baibai_engine.screening.run_store import ScreeningRunReader
from baibai_engine.screening.run_store.read import RunPublication
from baibai_engine.storage.application.paths import database_path
from baibai_engine.storage.application.read import connect_read_only


class ResearchInputsRequiredError(ValueError):
    def __init__(self, code: str, tickers: tuple[str, ...], reason: str) -> None:
        self.code, self.tickers, self.reason = code, tickers, reason
        super().__init__(reason)

    def public(self) -> dict[str, Any]:
        return {"code": self.code, "tickers": list(self.tickers), "reason": self.reason}


def _run(
    *, runs_path: Path, run_revision_id: str | None, research_as_of: date, known_at: datetime
) -> RunPublication | None:
    if known_at.utcoffset() is None or research_as_of > known_at.astimezone(JST).date():
        raise ValueError("Research date/cutoff is invalid")
    if not runs_path.exists():
        if run_revision_id is not None:
            raise ValueError(f"unknown run_revision_id: {run_revision_id}")
        return None
    reader = ScreeningRunReader(runs_path)
    return reader.analysis_run(
        as_of=research_as_of, known_at=known_at, run_revision_id=run_revision_id
    )


def load_analysis_snapshot(
    *,
    run_revision_id: str | None,
    research_as_of: date,
    tickers: tuple[str, ...],
    gaps: tuple[InputGap, ...],
    known_at: datetime,
    runs_path: Path,
) -> AnalysisInputSnapshot:
    # None means no source, never an implicit change to the draft's chosen run.
    run = (
        None
        if run_revision_id is None
        else _run(
            runs_path=runs_path,
            run_revision_id=run_revision_id,
            research_as_of=research_as_of,
            known_at=known_at,
        )
    )
    rows = {} if run is None else {row["ticker"]: row for row in run.stock_analyses}
    missing = set(tickers) - rows.keys()
    reasons = {gap.ticker: gap.reason for gap in gaps}
    if missing - reasons.keys():
        raise ResearchInputsRequiredError(
            "selected_analysis_missing",
            tuple(sorted(missing - reasons.keys())),
            "selected targets require analysis or an explicit InputGap",
        )
    if reasons.keys() != missing:
        raise ValueError("InputGap is permitted only for unavailable selected analyses")
    return AnalysisInputSnapshot(
        run_revision_id=None if run is None else run.run_revision_id,
        analysis_as_of=None if run is None else date.fromisoformat(run.as_of_date),
        run_at=None if run is None else datetime.fromisoformat(run.run_at),
        screening_rules_hash=None if run is None else run.payload.get("screening_rules_hash"),
        er_model_version=None if run is None else run.payload.get("er_model_version"),
        analyses={ticker: rows[ticker] for ticker in tickers if ticker in rows},
        unavailable=reasons,
    )


def verify_analysis_snapshot(
    snapshot: AnalysisInputSnapshot,
    *,
    tickers: tuple[str, ...],
    known_at: datetime,
    runs_path: Path,
) -> None:
    gaps = tuple(
        InputGap(ticker=ticker, reason=reason, attempted_sources=("saved source",))
        for ticker, reason in snapshot.unavailable.items()
    )
    canonical = load_analysis_snapshot(
        run_revision_id=snapshot.run_revision_id,
        research_as_of=known_at.astimezone(JST).date(),
        tickers=tickers,
        gaps=gaps,
        known_at=known_at,
        runs_path=runs_path,
    )
    if canonical != snapshot:
        raise ValueError("analysis snapshot differs from its canonical run")


def load_selection_context(
    *,
    request: ResearchRequest,
    research_as_of: date | None,
    run_revision_id: str | None,
    known_at: datetime,
    runs_path: Path,
    db_path: Path | None,
    comparison_tickers: tuple[str, ...] = (),
) -> dict[str, Any]:
    comparison_tickers = tuple(
        dict.fromkeys(normalize_ticker(t) for t in comparison_tickers),
    )
    as_of = research_as_of or known_at.astimezone(JST).date()
    run = _run(
        runs_path=runs_path,
        run_revision_id=run_revision_id,
        research_as_of=as_of,
        known_at=known_at,
    )
    missing: list[dict[str, Any]] = []
    candidates: list[dict[str, Any]] = []
    if run is None:
        missing.append(
            ResearchInputsRequiredError(
                "no_screening_run", (), "Screening Run is unavailable"
            ).public()
        )
    else:
        candidates = [dict(row) for row in candidate_rows(run.stock_analyses)]
    held: tuple[str, ...] = ()
    app_path = database_path(db_path)
    if app_path.exists():
        with closing(connect_read_only(app_path)) as connection:
            ledger = load_ledger_document(connection)
            if ledger is not None:
                state = replay_events_through(ledger.events, known_at)
                held = tuple(
                    sorted(
                        t for t, lots in state.lots.items() if any(lot.quantity > 0 for lot in lots)
                    )
                )
    requested = request.requested_tickers or (held if request.scope == "review_positions" else ())
    rows = {} if run is None else {row["ticker"]: row for row in run.stock_analyses}
    absent = tuple(ticker for ticker in requested if ticker not in rows)
    if absent:
        missing.append(
            ResearchInputsRequiredError(
                "selected_analysis_missing", absent, "requested analysis is unavailable"
            ).public()
        )
    context = latest_assessment(app_path, as_of=as_of, known_at=known_at)
    return {
        "schema_version": 2,
        "status": "inputs_required" if missing else "ready",
        "request": request.model_dump(mode="json"),
        "research_as_of": as_of.isoformat(),
        "analysis_as_of": None if run is None else run.as_of_date,
        "input_known_at": known_at.isoformat(),
        "run_revision_id": None if run is None else run.run_revision_id,
        "capacity": CURRENT_RESEARCH_CAPACITY.model_dump(mode="json"),
        "candidates": candidates,
        "candidate_discovery_method": None
        if run is None
        else run.payload["candidate_discovery"]["method"],
        "requested_analyses": {t: rows[t] for t in requested if t in rows},
        "comparison_analyses": {t: rows[t] for t in comparison_tickers if t in rows},
        "missing_comparison_tickers": [t for t in comparison_tickers if t not in rows],
        "held_tickers": list(held),
        "missing_inputs": missing,
        "saved_market_context_assessment": None
        if context is None
        else assessment_projection(context, as_of=as_of),
    }
