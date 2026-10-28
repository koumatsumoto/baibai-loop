"""保存済み入力の選定contextと初回照合。取得・書込・AI起動は行わない。"""

from __future__ import annotations

from contextlib import closing
from datetime import date, datetime
from pathlib import Path
from typing import Any

from baibai_engine.primitives.stocks import normalize_ticker
from baibai_engine.primitives.time import JST
from baibai_engine.investment_judgment.market_assessment.projection import assessment_projection
from baibai_engine.investment_judgment.market_assessment.query import latest_assessment
from baibai_engine.investment_judgment.workflow.selection import ResearchRequest
from baibai_engine.investment_judgment.workflow.selection_policy import CURRENT_RESEARCH_CAPACITY
from baibai_engine.portfolio.ledger.events import replay_events_through
from baibai_engine.portfolio.ledger.read import load_ledger_document
from baibai_engine.storage.application.paths import database_path
from baibai_engine.storage.application.read import connect_read_only


def load_generated_selection_context(
    *,
    request: ResearchRequest,
    generation_id: str | None,
    screening_selection_id: str | None,
    research_as_of: date | None,
    known_at: datetime,
    runs_path: Path,
    db_path: Path | None,
    comparison_tickers: tuple[str, ...] = (),
) -> dict[str, Any]:
    """保存された政策結果と同じ生成を読む。contextの取得で再選定しない。"""
    from pydantic import TypeAdapter

    from baibai_engine.investment_judgment.workflow.generated_inputs import project_generated_inputs
    from baibai_engine.machine_analysis.generation.query import read_model_inputs
    from baibai_engine.machine_analysis.screening.selection_query import (
        read_publication,
    )
    from baibai_engine.machine_analysis.screening.views import ViewSelection

    if known_at.utcoffset() is None:
        raise ValueError("input cutoff must have a timezone")
    as_of = research_as_of or known_at.astimezone(JST).date()
    if as_of > known_at.astimezone(JST).date():
        raise ValueError("Research date/cutoff is invalid")
    if generation_id is None and screening_selection_id is None:
        current = read_publication(path=runs_path, as_of=as_of, known_at=known_at)
        if current is None:
            return {
                "schema_version": 3,
                "status": "inputs_required",
                "request": request.model_dump(mode="json"),
                "research_as_of": as_of.isoformat(),
                "input_known_at": known_at.isoformat(),
                "missing_inputs": [{"code": "no_model_publication"}],
            }
        generation_id = current.generation.generation_id
        screening_selection_id = current.selection_id
    if request.scope == "discover_new" and screening_selection_id is None:
        raise ValueError("discovery requires an explicit saved screening selection")
    screening = None
    if screening_selection_id is not None:
        publication = read_publication(
            path=runs_path, selection_id=screening_selection_id, as_of=as_of, known_at=known_at
        )
        if publication is None:
            raise ValueError("unknown or not yet published screening selection")
        screening = publication.selection
        if generation_id is not None and generation_id != screening.generation_id:
            raise ValueError("screening selection differs from the chosen generation")
        generation_id = screening.generation_id
    if generation_id is None:
        raise ValueError("an explicit generation is required")
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
    candidates = () if screening is None else screening.result.tickers
    subjects = tuple(
        sorted(
            {
                "market",
                *requested,
                *candidates,
                *(normalize_ticker(ticker) for ticker in comparison_tickers),
            }
        )
    )
    models = read_model_inputs(generation_id, subjects=subjects, path=runs_path)
    if models is None:
        raise ValueError("chosen model generation is unavailable")
    if models.as_of > as_of or models.evaluation_time > known_at:
        raise ValueError("chosen generation is after the research cutoff")
    context = latest_assessment(app_path, as_of=as_of, known_at=known_at)
    return {
        "schema_version": 3,
        "status": "ready",
        "request": request.model_dump(mode="json"),
        "research_as_of": as_of.isoformat(),
        "input_known_at": known_at.isoformat(),
        "generation_id": generation_id,
        "screening_selection_id": screening_selection_id,
        "capacity": CURRENT_RESEARCH_CAPACITY.model_dump(mode="json"),
        "screening": None
        if screening is None
        else TypeAdapter(ViewSelection).dump_python(screening.result, mode="json"),
        "candidate_tickers": list(candidates),
        "candidate_order_semantics": "per_view_rank_not_a_union_investment_ranking",
        "requested_tickers": requested,
        "held_tickers": held,
        "model_inputs": project_generated_inputs(models),
        "saved_market_context_assessment": None
        if context is None
        else assessment_projection(context, as_of=as_of),
    }
