"""保存済み三領域を調査workspaceへ合成する。AI起動・取得・store書込は行わない。"""

from __future__ import annotations

from dataclasses import asdict
from datetime import date, datetime
from pathlib import Path

from baibai_engine.external_assessment.query import external_assessment
from baibai_engine.foundation.sqlite_read import read_store_if_written
from baibai_engine.market_context.assessment.projection import assessment_projection
from baibai_engine.market_context.assessment.query import latest_assessment
from baibai_engine.market_context.reading.query import reading_snapshot
from baibai_engine.market_context.security import build_security_market_context
from baibai_engine.market_context.snapshot import build_market_snapshot
from baibai_engine.screening.run_store import ScreeningRunReader
from baibai_engine.valuation.query import (
    cash_flow_from_analysis,
    machine_valuation_from_analysis,
)


def research_inputs(
    *,
    tickers: tuple[str, ...],
    as_of: date,
    cutoff: datetime,
    known_at: datetime,
    market_path: Path,
    runs_path: Path,
    indicators_path: Path,
    app_path: Path,
    run_revision_id: str | None = None,
) -> dict[str, object]:
    run = read_store_if_written(
        runs_path,
        lambda: ScreeningRunReader(runs_path).valuation_run(
            as_of=as_of, run_revision_id=run_revision_id
        ),
    )
    context = latest_assessment(app_path, as_of=as_of, known_at=known_at)
    securities: dict[str, object] = {}
    for ticker in tickers:
        analysis = (
            None
            if run is None
            else next(
                (item for item in run.security_analyses if item.get("ticker") == ticker), None
            )
        )
        securities[ticker] = {
            "machine_valuation": None
            if analysis is None
            else {
                "run_revision_id": run.run_revision_id if run is not None else None,
                "as_of": run.as_of_date if run is not None else None,
                "estimate": asdict(machine_valuation_from_analysis(analysis)),
            },
            "cash_flow": None if analysis is None else cash_flow_from_analysis(analysis),
            "market_context": build_security_market_context(
                sqlite_path=market_path, ticker=ticker, asof_date=as_of, self_range_window_bars=750
            ),
            "external_assessment": external_assessment(
                market_path, ticker=ticker, cutoff=cutoff, evaluated_at=cutoff
            ).model_dump(mode="json"),
        }
    return {
        "as_of": as_of.isoformat(),
        "received_cutoff": cutoff.isoformat(),
        "known_at": known_at.isoformat(),
        "market_context": {
            "market": build_market_snapshot(sqlite_path=market_path, asof_date=as_of),
            "reading": reading_snapshot(indicators_path, asof=as_of),
            "saved_market_context_assessment": None
            if context is None
            else assessment_projection(context, as_of=as_of),
        },
        "securities": securities,
    }
