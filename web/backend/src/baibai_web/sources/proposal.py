"""One read-only App snapshot for both saved proposal views."""

from __future__ import annotations

import sqlite3
from collections.abc import Sequence
from contextlib import closing
from datetime import datetime
from pathlib import Path
from typing import NamedTuple

from baibai_engine.investment_judgment.trade_proposal.query import (
    ProposalRecordReader,
    load_proposal_with_assessments,
    published_proposal_ids,
    saved_proposal_report_context,
)
from baibai_engine.primitives.time import JST
from baibai_engine.storage.sqlite.application.read import connect_read_only
from baibai_web.readmodel.builders.proposal import project_saved_proposal
from baibai_web.readmodel.proposal_models import (
    EstimatedModelChartView,
    LatestProposalView,
    ProposalHistoryView,
    SavedMarketContextView,
    SavedProposalView,
)

_READ_ERRORS = (OSError, ValueError, RuntimeError, sqlite3.Error)


class ProposalViews(NamedTuple):
    latest: LatestProposalView
    history: ProposalHistoryView


def read_proposal_views(db_path: Path, *, generated_at: datetime | None = None) -> ProposalViews:
    """Project a verified publication once; latest and history keep distinct status and order."""
    try:
        with closing(connect_read_only(db_path)) as connection:
            connection.execute("BEGIN")
            return _read_snapshot(connection, generated_at)
    except _READ_ERRORS:
        return _views(generated_at)


def _read_snapshot(connection: sqlite3.Connection, generated_at: datetime | None) -> ProposalViews:
    history_ids: tuple[str, ...] | None
    try:
        history_ids = published_proposal_ids(connection)
    except _READ_ERRORS:
        history_ids = None
    try:
        records = ProposalRecordReader(connection)
    except _READ_ERRORS:
        # Even when the shared corrections are unreadable, retain the known history IDs.
        return _views(generated_at, missing=history_ids)
    latest_unavailable = False
    try:
        latest_id = records.latest_id()
    except _READ_ERRORS:
        latest_id = None
        latest_unavailable = True

    # The official latest is not the first row of history. If only the history
    # listing failed, read the selected publication without inventing a history.
    ids = history_ids if history_ids is not None else (() if latest_id is None else (latest_id,))
    projected: dict[str, SavedProposalView] = {}
    for proposal_id in ids:
        try:
            projected[proposal_id] = read_saved_proposal_view(connection, proposal_id, records)
        except _READ_ERRORS:
            continue

    latest = None if latest_id is None else projected.get(latest_id)
    missing = None if history_ids is None else [key for key in history_ids if key not in projected]
    return _views(
        generated_at,
        latest=latest,
        latest_unavailable=latest_unavailable or (latest_id is not None and latest is None),
        history=sorted(
            projected.values(), key=lambda row: (row.published_at, row.proposal_id), reverse=True
        )
        if history_ids is not None
        else (),
        missing=missing,
    )


def _views(
    generated_at: datetime | None,
    *,
    latest: SavedProposalView | None = None,
    latest_unavailable: bool = True,
    history: Sequence[SavedProposalView] = (),
    missing: Sequence[str] | None = None,
) -> ProposalViews:
    # Stamp the projection, not an instant before the first SQLite read pins its snapshot.
    if generated_at is None:
        generated_at = datetime.now(JST)
    return ProposalViews(
        LatestProposalView(
            generated_at=generated_at,
            status="unavailable" if latest_unavailable else "empty" if latest is None else "ready",
            error="保存済み提案を読み取れません。" if latest_unavailable else None,
            proposal=latest,
        ),
        ProposalHistoryView(
            generated_at=generated_at,
            status="unavailable"
            if missing is None
            else ("partial" if history else "unavailable")
            if missing
            else ("ready" if history else "empty"),
            error="保存済み提案の履歴を読み取れません。"
            if missing is None
            else "一部の保存済み提案を読み取れません。"
            if missing
            else None,
            proposals=list(history),
            unavailable_proposal_ids=list(missing or ()),
        ),
    )


def read_saved_proposal_view(
    connection: sqlite3.Connection, proposal_id: str, records: ProposalRecordReader
) -> SavedProposalView:
    proposal, pairs = load_proposal_with_assessments(connection, proposal_id)
    view = project_saved_proposal(proposal, pairs)
    view.corrections, view.human_reports = records.read(proposal)
    context = saved_proposal_report_context(connection, proposal, pairs)
    view.saved_market_context = SavedMarketContextView.model_validate(context.market)
    for item in view.items:
        if item.ticker in context.issuer_names:
            item.company_name, item.sector = context.issuer_names[item.ticker]
        assessment = item.assessment
        if assessment is not None and assessment.assessment_id in context.charts:
            item.assessment = assessment.model_copy(
                update={
                    "estimated_model_chart": EstimatedModelChartView.model_validate(
                        context.charts[assessment.assessment_id]
                    ),
                }
            )
    return view
