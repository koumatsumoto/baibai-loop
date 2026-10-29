"""時点を固定した再訪材料と、Research/Assessment企業の有限catalog。"""

from __future__ import annotations

import sqlite3
from contextlib import closing
from datetime import date, datetime
from pathlib import Path

from baibai_engine.investment_judgment.company_research.context import (
    AvailableAssessment,
    AvailableResearch,
    CompanyContext,
    NotFound,
    Unreadable,
    company_context_brief,
)
from baibai_engine.investment_judgment.company_research.store import (
    ResearchReadError,
    latest_research_id,
    load_research,
)
from baibai_engine.investment_judgment.contracts import _datetime
from baibai_engine.investment_judgment.stock_assessment.document import AssessmentRef
from baibai_engine.investment_judgment.stock_assessment.store import (
    load_reviewed_assessment,
)
from baibai_engine.primitives.stocks import normalize_ticker
from baibai_engine.storage.sqlite.application.paths import database_path
from baibai_engine.storage.sqlite.application.read import connect_read_only


def _assessment_context(
    connection: sqlite3.Connection, ticker: str, *, as_of: date, known_at: datetime
) -> AvailableAssessment | NotFound | Unreadable:
    # Select headers first: unreadable latest content must not expose an older stance.
    identity = None
    try:
        headers: list[tuple[date, datetime, str]] = []
        for row in connection.execute(
            "SELECT assessment_id, as_of, published_at FROM stock_assessment WHERE ticker=?",
            (ticker,),
        ):
            information_date, instant = date.fromisoformat(str(row[1])), _datetime(str(row[2]))
            if information_date <= as_of and instant <= known_at:
                headers.append((information_date, instant, str(row[0])))
        if not headers:
            return NotFound()
        _, published_at, identity = max(headers)
        pair = load_reviewed_assessment(connection, identity)
        snapshot = pair.document.input_snapshot
        return AvailableAssessment(
            reference=AssessmentRef(
                assessment_id=identity,
                core_sha256=pair.core_sha256,
                review_id=pair.review.review_id,
            ),
            ticker=snapshot.ticker,
            as_of=snapshot.as_of,
            published_at=published_at,
            stance=pair.document.conclusion.stance,
            sources=snapshot.sources,
            facts=snapshot.facts,
            findings=pair.document.findings,
        )
    except ValueError as error:
        return Unreadable(record_id=identity, reason=str(error))


def company_context(
    connection: sqlite3.Connection, ticker: str, *, as_of: date, known_at: datetime
) -> CompanyContext:
    _datetime(known_at)
    identity = None
    research: AvailableResearch | NotFound | Unreadable
    try:
        identity = latest_research_id(connection, ticker, as_of=as_of, known_at=known_at)
        research = (
            NotFound()
            if identity is None
            else AvailableResearch(record=load_research(connection, identity))
        )
    except ResearchReadError as error:
        research = Unreadable(record_id=identity, reason=str(error))
    return CompanyContext(
        research=research,
        assessment=_assessment_context(connection, ticker, as_of=as_of, known_at=known_at),
    )


def read_company_contexts(
    path: Path | None, *, tickers: tuple[str, ...], as_of: date, known_at: datetime
) -> dict[str, CompanyContext]:
    _datetime(known_at)
    subjects = sorted({normalize_ticker(ticker) for ticker in tickers})
    resolved = database_path(path)
    if not resolved.exists():
        return {
            ticker: CompanyContext(research=NotFound(), assessment=NotFound())
            for ticker in subjects
        }
    with closing(connect_read_only(resolved)) as connection:
        if connection.execute("PRAGMA user_version").fetchone()[0] == 0:
            return {
                ticker: CompanyContext(research=NotFound(), assessment=NotFound())
                for ticker in subjects
            }
        # One read snapshot for both owners, never a latest lookup during resume.
        connection.execute("BEGIN")
        return {
            ticker: company_context(connection, ticker, as_of=as_of, known_at=known_at)
            for ticker in subjects
        }


def research_catalog(
    path: Path | None,
    *,
    as_of: date,
    known_at: datetime,
    after_ticker: str | None = None,
    limit: int = 20,
) -> dict[str, object]:
    _datetime(known_at)
    if not 1 <= limit <= 100:
        raise ValueError("catalog limit must be between 1 and 100")
    cursor = "" if after_ticker is None else normalize_ticker(after_ticker)
    resolved = database_path(path)
    items: list[dict[str, object]] = []
    next_cursor: str | None = None
    if resolved.exists():
        with closing(connect_read_only(resolved)) as connection:
            connection.execute("BEGIN")
            if connection.execute("PRAGMA user_version").fetchone()[0] == 0:
                return {
                    "as_of": as_of.isoformat(),
                    "known_at": known_at.isoformat(),
                    "items": [],
                    "next_after_ticker": None,
                    "order": "ticker",
                    "limit": limit,
                }
            # Page by ticker, not score. Future-only companies are not historical research.
            rows = connection.execute(
                "SELECT ticker FROM (SELECT ticker FROM company_research "
                "UNION SELECT ticker FROM stock_assessment) "
                "WHERE ticker > ? ORDER BY ticker",
                (cursor,),
            )
            for row in rows:
                ticker = str(row[0])
                context = company_context(connection, ticker, as_of=as_of, known_at=known_at)
                if isinstance(context.research, NotFound) and isinstance(
                    context.assessment, NotFound
                ):
                    continue
                if len(items) == limit:
                    next_cursor = str(items[-1]["ticker"])
                    break
                items.append({"ticker": ticker, **company_context_brief(context)})
    return {
        "as_of": as_of.isoformat(),
        "known_at": known_at.isoformat(),
        "items": items,
        "next_after_ticker": next_cursor,
        "order": "ticker",
        "limit": limit,
    }
