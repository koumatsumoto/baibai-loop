"""Build a minimal Capital Allocation Assessment draft from canonical records."""

from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from datetime import date, datetime
from pathlib import Path

from baibai_engine.appdb.paths import database_path
from baibai_engine.appdb.read import connect_read_only
from baibai_engine.operation.research_binding import require_active_research_set

from .capital_allocation import CapitalAllocationConflictError
from .thesis import require_recorded_identity

UNREVIEWED_DRAFT_SHA256 = "0" * 64
_TODO = "TODO"


def scaffold_capital_allocation(
    *,
    db_path: Path | None,
    capital_allocation_assessment_id: str,
    as_of: date,
    research_triage_id: str,
    thesis_ids: list[str],
    published_at: datetime,
) -> dict[str, object]:
    if not thesis_ids:
        raise CapitalAllocationConflictError("at least one promoted thesis is required")
    path = database_path(db_path)
    if not path.is_file():
        raise CapitalAllocationConflictError(f"application database is unavailable: {path}")
    with closing(connect_read_only(path)) as connection:
        triage = _payload(
            connection,
            "SELECT payload FROM research_triage WHERE research_triage_id = ?",
            research_triage_id,
        )
        raw_entries = triage.get("entries")
        if not isinstance(raw_entries, list):
            raise CapitalAllocationConflictError("research triage entries are invalid")
        researchable = {
            str(entry["ticker"])
            for entry in raw_entries
            if isinstance(entry, dict) and entry.get("decision") == "research"
        }
        alternatives = [
            _alternative(connection, thesis_id, researchable) for thesis_id in thesis_ids
        ]
        try:
            require_active_research_set(
                connection,
                research_triage_id=research_triage_id,
                tickers=(str(item["ticker"]) for item in alternatives),
                published_at=published_at,
            )
        except ValueError as error:
            raise CapitalAllocationConflictError(str(error)) from error
    macro_context_id = triage.get("macro_context_id")
    return {
        "schema_version": 1,
        "kind": "capital_allocation_assessment",
        "capital_allocation_assessment_id": capital_allocation_assessment_id,
        "as_of": as_of.isoformat(),
        "published_at": published_at.isoformat(),
        "result": "no_allocation",
        "headline": _TODO,
        "research_triage_id": research_triage_id,
        "macro_context_id": macro_context_id if isinstance(macro_context_id, str) else None,
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
    researchable: set[str],
) -> dict[str, object]:
    row = connection.execute(
        "SELECT ticker, core_sha256 FROM thesis WHERE thesis_id = ?", (thesis_id,)
    ).fetchone()
    if row is None:
        raise CapitalAllocationConflictError(f"thesis is unavailable: {thesis_id}")
    ticker = str(row[0])
    if ticker not in researchable:
        raise CapitalAllocationConflictError(
            f"thesis ticker is not researchable in the ResearchTriage: {ticker}"
        )
    return {
        "ticker": ticker,
        "thesis_id": thesis_id,
        "thesis_core_sha256": require_recorded_identity(row[1], thesis_id),
        "thesis_review_id": None,
        "disposition": "decline",
        "rationale": _TODO,
    }


def _payload(connection: sqlite3.Connection, sql: str, identifier: str) -> dict[str, object]:
    row = connection.execute(sql, (identifier,)).fetchone()
    if row is None:
        raise CapitalAllocationConflictError(f"source is unavailable: {identifier}")
    payload = json.loads(str(row[0]))
    if not isinstance(payload, dict):
        raise CapitalAllocationConflictError(f"source payload is invalid: {identifier}")
    return payload


__all__ = ["UNREVIEWED_DRAFT_SHA256", "scaffold_capital_allocation"]
