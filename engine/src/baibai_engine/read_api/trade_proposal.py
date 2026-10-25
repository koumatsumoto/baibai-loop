"""保存提案の読取と現在 preview を明示的に別の値として提供する。"""

from __future__ import annotations

from contextlib import closing
from datetime import datetime
from pathlib import Path

from baibai_engine.investment_judgment.stock_assessment_store import require_assessment_ref
from baibai_engine.investment_judgment.trade_proposal_query import (
    load_investment_proposal,
    preview_current_terms,
)
from baibai_engine.storage.application.read import connect_read_only

from .stock_assessment import reviewed_assessment_projection
from .sqlite import read_application_rows as read_rows


def list_investment_proposal_payloads(path: Path) -> list[dict[str, object]]:
    rows = read_rows(path, "SELECT proposal_id FROM investment_proposal")
    if not rows:
        return []
    with closing(connect_read_only(path)) as connection:
        connection.execute("BEGIN")
        proposals = [load_investment_proposal(connection, str(row[0])) for row in rows]
    return [
        proposal.model_dump(mode="json")
        for proposal in sorted(
            proposals,
            key=lambda proposal: (proposal.as_of, proposal.published_at, proposal.proposal_id),
            reverse=True,
        )
    ]


def investment_proposal_payload(path: Path, *, proposal_id: str) -> dict[str, object] | None:
    rows = read_rows(
        path, "SELECT proposal_id FROM investment_proposal WHERE proposal_id=?", (proposal_id,)
    )
    if not rows:
        return None
    with closing(connect_read_only(path)) as connection:
        connection.execute("BEGIN")
        proposal = load_investment_proposal(connection, proposal_id)
        items: list[dict[str, object]] = []
        for item in proposal.items:
            raw = item.model_dump(mode="json")
            if item.assessment_ref is not None:
                pair = require_assessment_ref(connection, item.assessment_ref)
                raw["assessment_projection"] = reviewed_assessment_projection(
                    path,
                    assessment_id=pair.assessment_id,
                    assessment_ref=item.assessment_ref,
                )
                raw["assessment_review"] = pair.review.model_dump(mode="json")
            items.append(raw)
    return {**proposal.model_dump(mode="json"), "items": items}


def preview_current_proposal_terms(
    path: Path, *, proposal_id: str, sqlite_path: Path, now: datetime
) -> dict[str, object] | None:
    rows = read_rows(
        path, "SELECT proposal_id FROM investment_proposal WHERE proposal_id=?", (proposal_id,)
    )
    if not rows:
        return None
    return preview_current_terms(
        proposal_id=proposal_id, db_path=path, sqlite_path=sqlite_path, now=now
    ).payload()
