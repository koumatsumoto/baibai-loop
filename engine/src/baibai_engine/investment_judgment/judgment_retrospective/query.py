"""当時判断・後日証拠・記録済み取引を別々にexact読取する。"""

from __future__ import annotations

from contextlib import closing
from datetime import date, datetime
from pathlib import Path

from baibai_engine.investment_judgment.contracts import _datetime
from baibai_engine.investment_judgment.judgment_retrospective.context import (
    assessment_reference,
    capture_ledger_snapshot,
    ledger_context,
    origin_context,
    retrospective_context,
)
from baibai_engine.investment_judgment.judgment_retrospective.document import (
    ProposalItemRef,
    RetrospectiveOrigin,
)
from baibai_engine.investment_judgment.judgment_retrospective.store import (
    RetrospectiveReadError,
    load_retrospective,
    validate_correction,
)
from baibai_engine.investment_judgment.stock_assessment.store import (
    load_reviewed_assessment,
)
from baibai_engine.investment_judgment.trade_proposal.contracts import proposal_draft_sha256
from baibai_engine.investment_judgment.trade_proposal.query import load_investment_proposal
from baibai_engine.portfolio.ledger.read import (
    optional_ledger_head,
)
from baibai_engine.primitives.stocks import normalize_ticker
from baibai_engine.primitives.time import JST
from baibai_engine.storage.sqlite.application.paths import database_path
from baibai_engine.storage.sqlite.application.read import connect_read_only


def scaffold_retrospective(
    *,
    retrospective_id: str,
    as_of: date,
    ticker: str | None = None,
    proposal_id: str | None = None,
    assessment_id: str | None = None,
    milestone: str = "",
    db_path: Path | None = None,
    known_at: datetime | None = None,
) -> dict[str, object]:
    if (proposal_id is None) == (assessment_id is None):
        raise ValueError("choose exactly one origin: proposal_id or assessment_id")
    now = _datetime(known_at or datetime.now(JST))
    with closing(connect_read_only(db_path)) as connection:
        connection.execute("BEGIN")
        origin: RetrospectiveOrigin
        head = None
        if proposal_id is not None:
            if ticker is None:
                raise ValueError("Proposal origin requires ticker")
            ticker = normalize_ticker(ticker)
            proposal = load_investment_proposal(connection, proposal_id)
            origin = ProposalItemRef(
                proposal_id=proposal_id,
                draft_sha256=proposal_draft_sha256(proposal),
                ticker=ticker,
            )
            head = optional_ledger_head(connection)
        else:
            assert assessment_id is not None
            pair = load_reviewed_assessment(connection, assessment_id)
            origin = assessment_reference(pair)
            actual_ticker = pair.document.input_snapshot.ticker
            if ticker is not None and normalize_ticker(ticker) != actual_ticker:
                raise ValueError("origin Assessment ticker mismatch")
            ticker = actual_ticker
        ledger = capture_ledger_snapshot(
            connection, origin=origin, ticker=ticker, as_of=as_of, append_head=head
        )
        context: dict[str, object] = {
            "origin": origin_context(
                connection, origin, ticker=ticker, as_of=as_of, recorded_at=now
            ),
            "ledger": ledger_context(connection, ledger, as_of=as_of),
            "citations": [],
        }
        # This is deliberately an unfinished JSON draft, not a valid empty document.
        return {
            "draft": {
                "retrospective_id": retrospective_id,
                "ledger_append_head": head,
                "document": {
                    "kind": "judgment_retrospective",
                    "schema_version": 1,
                    "ticker": ticker,
                    "as_of": as_of.isoformat(),
                    "milestone": milestone,
                    "origin": origin.model_dump(mode="json"),
                    "comparisons": [],
                    "lesson": None,
                    "corrects_ref": None,
                },
            },
            "context": context,
        }


def get_retrospective(
    *, retrospective_id: str, db_path: Path | None = None, sha256: str | None = None
) -> dict[str, object]:
    with closing(connect_read_only(db_path)) as connection:
        connection.execute("BEGIN")
        record = load_retrospective(connection, retrospective_id)
        if sha256 is not None and record.reference.payload_sha256 != sha256:
            raise RetrospectiveReadError("Retrospective reference differs from stored content")
        validate_correction(connection, record.document, recorded_at=record.recorded_at)
        return {
            "record": record.model_dump(mode="json"),
            "context": retrospective_context(connection, record),
        }


def list_retrospectives(
    *, db_path: Path | None = None, ticker: str | None = None, limit: int = 20
) -> dict[str, object]:
    if not 1 <= limit <= 100:
        raise ValueError("retrospective limit must be between 1 and 100")
    if ticker is not None:
        ticker = normalize_ticker(ticker)
    items: list[dict[str, object]] = []
    has_more = False
    resolved = database_path(db_path)
    if resolved.exists():
        with closing(connect_read_only(resolved)) as connection:
            connection.execute("BEGIN")
            if connection.execute("PRAGMA user_version").fetchone()[0] != 0:
                if ticker is None:
                    statement = (
                        "SELECT retrospective_id FROM judgment_retrospective "
                        "ORDER BY recorded_at DESC,retrospective_id DESC LIMIT ?"
                    )
                    parameters: tuple[object, ...] = (limit + 1,)
                else:
                    statement = (
                        "SELECT retrospective_id FROM judgment_retrospective WHERE ticker=? "
                        "ORDER BY recorded_at DESC,retrospective_id DESC LIMIT ?"
                    )
                    parameters = (ticker, limit + 1)
                rows = connection.execute(statement, parameters).fetchall()
                has_more = len(rows) > limit
                for row in rows[:limit]:
                    record = load_retrospective(connection, str(row[0]))
                    document = record.document
                    items.append(
                        {
                            "reference": record.reference.model_dump(mode="json"),
                            "ticker": document.ticker,
                            "as_of": document.as_of.isoformat(),
                            "recorded_at": record.recorded_at.isoformat(),
                            "origin": document.origin.model_dump(mode="json"),
                            "milestone": document.milestone,
                            "lesson": None
                            if document.lesson is None
                            else document.lesson.next_time[:240],
                            "corrects_ref": None
                            if document.corrects_ref is None
                            else document.corrects_ref.model_dump(mode="json"),
                        }
                    )
    return {"items": items, "has_more": has_more, "limit": limit, "order": "recorded_at_desc"}
