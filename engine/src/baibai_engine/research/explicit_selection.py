"""ユーザー指定の開始時に保存runの根拠を固定する。候補やTriageは生成しない。"""

from __future__ import annotations

from datetime import date, datetime
from pathlib import Path

from baibai_engine.foundation.time import JST
from baibai_engine.research.operation.progress import ExplicitResearchRun
from baibai_engine.screening.run_store import ScreeningRunReader


def load_explicit_research_run(
    path: Path,
    *,
    run_revision_id: str,
    tickers: tuple[str, ...],
    known_at: datetime,
    as_of: date | None = None,
) -> ExplicitResearchRun:
    """固定runを読取のみで解決し、開始時点で利用できた銘柄分析を引用する。"""
    run = ScreeningRunReader(path).get_run(run_revision_id)
    if run is None:
        raise ValueError(f"unknown run_revision_id: {run_revision_id}")
    run_date = date.fromisoformat(run.as_of_date)
    if as_of is not None and as_of != run_date:
        raise ValueError("--asof differs from the explicitly selected analysis run")
    run_at = datetime.fromisoformat(run.run_at)
    if run_at.utcoffset() is None or known_at.utcoffset() is None:
        raise ValueError("explicit run and start time must include a timezone")
    if run_at > known_at or run_date > known_at.astimezone(JST).date():
        raise ValueError("explicit analysis run is after the Research start cutoff")
    analyses = {row["ticker"]: row for row in run.security_analyses}
    missing = set(tickers) - analyses.keys()
    if missing:
        raise ValueError(f"selected run has no Security Analysis for {', '.join(sorted(missing))}")
    return ExplicitResearchRun(
        run_revision_id=run.run_revision_id,
        as_of=run_date,
        run_at=run_at,
        run_metadata=run.payload,
        analyses={ticker: analyses[ticker] for ticker in tickers},
    )


def verify_explicit_research_run(
    source: ExplicitResearchRun, *, path: Path, as_of: date, known_at: datetime
) -> None:
    canonical = load_explicit_research_run(
        path,
        run_revision_id=source.run_revision_id,
        tickers=tuple(source.analyses),
        known_at=known_at,
        as_of=as_of,
    )
    if canonical.binding() != source.binding():
        raise ValueError("explicit selection snapshot differs from the canonical analysis run")
