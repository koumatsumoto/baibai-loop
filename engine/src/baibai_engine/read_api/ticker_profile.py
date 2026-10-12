"""Produce a deterministic single-ticker fact profile from canonical stores.

The profile is the entry point for AI research on one security: price and
liquidity facts for any listed ticker (inside or outside the screening
universe), benchmark- and sector-relative momentum, the market benchmark_trend at the
evaluation date, event facts (next earnings, JPX
regulation), the ticker's latest recorded Security Analysis, and prior research
decisions. Every field is a deterministic transform of stored data; the profile
contains no interpretation and no composite score. The portfolio block retains
its independent ledger_as_of; this is not an as-of replay of holdings.

Valuation metrics are quoted from the Screening Run rather than recomputed, so
the profile never disagrees with its Security Analysis. Absence is explicit.
"""

from __future__ import annotations

from datetime import date, datetime, time
from pathlib import Path
from zoneinfo import ZoneInfo

from baibai_engine.external_assessment.query import external_assessment
from baibai_engine.foundation.sqlite_read import read_rows
from baibai_engine.market_context.benchmark import NIKKEI225_ETF_PROXY
from baibai_engine.market_context.security import build_security_market_context
from baibai_engine.read_api.position import (
    portfolio_ledger_document,
    replay_events_through,
    summarize_portfolio,
)
from baibai_engine.screening.run_store import ScreeningRunReader
from baibai_engine.storage.observations.reader import read_jpx_earnings_calendar_snapshot
from baibai_engine.valuation.metrics.history import VALUATION_HISTORY_SESSIONS


def build_ticker_profile(
    *,
    sqlite_path: Path,
    ticker: str,
    asof_date: date,
    runs_db_path: Path,
    app_db_path: Path,
    run_revision_id: str | None = None,
    benchmark_ticker: str = NIKKEI225_ETF_PROXY,
) -> dict[str, object]:
    """Assemble the deterministic fact profile for ``ticker``."""
    market_context = build_security_market_context(
        sqlite_path=sqlite_path,
        ticker=ticker,
        asof_date=asof_date,
        self_range_window_bars=VALUATION_HISTORY_SESSIONS,
        benchmark_ticker=benchmark_ticker,
    )
    master = market_context["master"]
    sector = master.get("sector_33") if isinstance(master, dict) else None
    screening_context = _load_security_analysis_context(
        runs_db_path,
        ticker,
        asof_date,
        run_revision_id=run_revision_id,
    )
    cutoff = datetime.combine(asof_date, time.max, ZoneInfo("Asia/Tokyo"))
    return {
        "external_assessment": external_assessment(
            sqlite_path,
            ticker=ticker,
            cutoff=cutoff,
            evaluated_at=cutoff,
        ).model_dump(mode="json"),
        "ticker": ticker,
        "asof": asof_date.isoformat(),
        **market_context,
        "events": {
            "next_earnings_date": _next_earnings_date(sqlite_path, ticker, asof_date),
            "jpx_regulation": _jpx_flags(sqlite_path, ticker, asof_date),
        },
        "screening": screening_context,
        "portfolio": _portfolio_block(
            app_db_path=app_db_path,
            ticker=ticker,
            sector=sector if isinstance(sector, str) else None,
        ),
    }


def _portfolio_block(
    *,
    app_db_path: Path,
    ticker: str,
    sector: str | None,
) -> dict[str, object]:
    """Show confirmed holdings and cost concentration without requiring valuation."""
    document = portfolio_ledger_document(app_db_path)
    if document is None:
        return _empty_portfolio_block()
    state = replay_events_through(document.events, document.as_of)
    snapshot = summarize_portfolio(document, state, {})
    positions = []
    total_notional = 0
    same_sector_notional = 0
    holds_this_ticker = False
    for holding in snapshot.holdings:
        notional = holding.deployed_cost_yen
        total_notional += notional
        if holding.ticker == ticker:
            holds_this_ticker = True
        if sector is not None and holding.sector == sector:
            same_sector_notional += notional
        positions.append(
            {
                "ticker": holding.ticker,
                "name": None,
                "sector_33": holding.sector,
                "entry_notional_yen": notional,
            }
        )
    return {
        "ledger_as_of": document.as_of.isoformat(),
        "open_position_count": len(positions),
        "holds_this_ticker": holds_this_ticker,
        "same_sector_position_count": sum(
            1 for position in positions if sector is not None and position["sector_33"] == sector
        ),
        "same_sector_entry_notional_share": (
            round(same_sector_notional / total_notional, 4) if total_notional else None
        ),
        "open_positions": positions,
    }


def _empty_portfolio_block() -> dict[str, object]:
    return {
        "ledger_as_of": None,
        "open_position_count": 0,
        "holds_this_ticker": False,
        "same_sector_position_count": 0,
        "same_sector_entry_notional_share": None,
        "open_positions": [],
    }


def _load_security_analysis_context(
    runs_db_path: Path,
    ticker: str,
    asof_date: date,
    *,
    run_revision_id: str | None,
) -> dict[str, object]:
    if not runs_db_path.is_file():
        return {
            "screening_run_revision_id": None,
            "has_security_analysis": False,
            "note": "no screening run on or before asof",
        }
    reader = ScreeningRunReader(runs_db_path)
    run = reader.valuation_run(as_of=asof_date, run_revision_id=run_revision_id)
    if run is None:
        if run_revision_id is not None:
            raise ValueError(f"unknown run_revision_id: {run_revision_id}")
        return {
            "screening_run_revision_id": None,
            "has_security_analysis": False,
            "note": "no screening run on or before asof",
        }
    security_analyses = run.security_analyses
    security_analysis = None
    if security_analyses:
        security_analysis = next(
            (item for item in security_analyses if item.get("ticker") == ticker),
            None,
        )
    block: dict[str, object] = {
        "screening_run_revision_id": run.run_revision_id,
        "screening_run_as_of": run.as_of_date,
        "has_security_analysis": security_analysis is not None,
    }
    if security_analysis is not None:
        block["security_analysis"] = dict(security_analysis)
    else:
        block["note"] = (
            "ticker has no Security Analysis in the recorded Screening Run "
            "(outside the recorded run scope or no retained Security Analysis)"
        )
    return block


def _next_earnings_date(sqlite_path: Path, ticker: str, asof_date: date) -> str | None:
    snapshot = read_jpx_earnings_calendar_snapshot(sqlite_path, asof_date)
    if snapshot is None:
        return None
    next_date = min(
        (
            entry.announcement_date
            for entry in snapshot.entries
            if entry.ticker == ticker and entry.announcement_date >= asof_date
        ),
        default=None,
    )
    return next_date.isoformat() if next_date is not None else None


def _jpx_flags(sqlite_path: Path, ticker: str, asof_date: date) -> dict[str, object]:
    snapshot_row = _query_one(
        sqlite_path,
        "SELECT MAX(asof_date) FROM jpx_regulation_flags WHERE asof_date <= ?",
        (asof_date.isoformat(),),
    )
    snapshot_date = snapshot_row[0] if snapshot_row and isinstance(snapshot_row[0], str) else None
    if snapshot_date is None:
        return {"snapshot_date": None, "flags": []}
    rows = _query_all(
        sqlite_path,
        "SELECT DISTINCT flag FROM jpx_regulation_flags WHERE ticker = ? AND asof_date = ?",
        (ticker, snapshot_date),
    )
    return {
        "snapshot_date": snapshot_date,
        "flags": sorted(str(row[0]) for row in rows if row[0]),
    }


def _query_one(
    sqlite_path: Path,
    query: str,
    params: tuple[object, ...],
) -> tuple[object, ...] | None:
    rows = _query_all(sqlite_path, query, params)
    return rows[0] if rows else None


def _query_all(
    sqlite_path: Path,
    query: str,
    params: tuple[object, ...],
) -> list[tuple[object, ...]]:
    return [tuple(row) for row in read_rows(sqlite_path, query, params)]
