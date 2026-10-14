"""Build a minimal Capital Allocation Assessment draft from canonical records."""

from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from datetime import date, datetime
from pathlib import Path

from baibai_engine.research.operation.models import OperationSession
from baibai_engine.research.operation.progress import progress_binding, selection_binding
from baibai_engine.research.operation.research_binding import require_matching_research_set
from baibai_engine.storage.application.paths import database_path
from baibai_engine.storage.application.read import connect_read_only

from .capital_allocation import CapitalAllocationConflictError
from .thesis_store import load_reviewed_thesis

UNREVIEWED_DRAFT_SHA256 = "0" * 64
_TODO = "TODO"


def scaffold_capital_allocation(
    *,
    db_path: Path | None,
    capital_allocation_assessment_id: str,
    as_of: date,
    operation_id: str,
    published_at: datetime,
) -> dict[str, object]:
    path = database_path(db_path)
    with closing(connect_read_only(path)) as connection:
        connection.execute("BEGIN")
        row = connection.execute(
            "SELECT * FROM operation_session WHERE operation_id=?", (operation_id,)
        ).fetchone()
        if row is None:
            raise CapitalAllocationConflictError("Research Operation is unavailable")
        operation = OperationSession.model_validate(
            {**dict(row), "payload": json.loads(row["payload"])}
        )
        selection = selection_binding(operation.payload)
        if selection is None:
            raise CapitalAllocationConflictError("fixed Research selection is required")
        completed = [
            item
            for item in progress_binding(operation.payload).outcomes
            if item.status == "reviewed"
        ]
        alternatives = [_alternative(connection, item.thesis_id) for item in completed]
        try:
            scope = require_matching_research_set(
                operation,
                research_triage_id=None,
                tickers=(str(item["ticker"]) for item in alternatives),
                published_at=published_at,
            )
        except ValueError as error:
            raise CapitalAllocationConflictError(str(error)) from error
    context = selection.inputs.market_context.get("saved_market_context_assessment")
    market_context_assessment_id = context.get("context_id") if isinstance(context, dict) else None
    return {
        **({"research_scope": scope.model_dump(mode="json")} if scope is not None else {}),
        "schema_version": 2,
        "kind": "capital_allocation_assessment",
        "capital_allocation_assessment_id": capital_allocation_assessment_id,
        "as_of": as_of.isoformat(),
        "published_at": published_at.isoformat(),
        "result": "no_allocation",
        "headline": _TODO,
        "research_triage_id": None,
        "market_context_assessment_id": market_context_assessment_id
        if isinstance(market_context_assessment_id, str)
        else None,
        "comparison": _TODO,
        "forgone": _TODO,
        "alternatives": alternatives,
        "review": {
            "attempt": 1,
            "reviewer_identity": _TODO,
            "reviewed_at": published_at.isoformat(),
            "conclusion": "pass",
            "draft_sha256": UNREVIEWED_DRAFT_SHA256,
            "open_findings": [],
        },
    }


def _alternative(
    connection: sqlite3.Connection,
    thesis_id: str,
) -> dict[str, object]:
    pair = load_reviewed_thesis(connection, thesis_id)
    ticker = pair.document.input_snapshot.ticker
    return {
        "ticker": ticker,
        "thesis_id": thesis_id,
        "thesis_core_sha256": pair.core_sha256,
        "thesis_review_id": pair.review.review_id,
        "disposition": "decline",
        "rationale": _TODO,
    }


__all__ = ["UNREVIEWED_DRAFT_SHA256", "scaffold_capital_allocation"]
