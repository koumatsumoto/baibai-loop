"""正式保存提案を単一read-only transactionで読む。市場・台帳の現在値は取得しない。"""

import sqlite3
from contextlib import closing
from pathlib import Path

from baibai_engine.investment_judgment.trade_proposal.query import (
    ProposalRecordReader,
    load_proposal_with_assessments,
    published_proposal_ids,
    saved_proposal_report_context,
)
from baibai_engine.storage.sqlite.application.read import connect_read_only
from baibai_web.readmodel.builders.proposal import project_saved_proposal
from baibai_web.readmodel.proposal_models import (
    EstimatedModelChartView,
    SavedMarketContextView,
    SavedProposalView,
)
from baibai_web.sources.protocols import ProposalHistoryRead


class DbProposalSource:
    def __init__(self, db_path: Path) -> None:
        self._path = db_path

    def latest(self) -> SavedProposalView | None:
        with closing(connect_read_only(self._path)) as connection:
            connection.execute("BEGIN")
            records = ProposalRecordReader(connection)
            proposal_id = records.latest_id()
            return None if proposal_id is None else _project(connection, proposal_id, records)

    def history(self) -> ProposalHistoryRead:
        with closing(connect_read_only(self._path)) as connection:
            connection.execute("BEGIN")
            proposals = []
            unavailable = []
            ids = published_proposal_ids(connection)
            if not ids:
                return ProposalHistoryRead((), ())
            try:
                records = ProposalRecordReader(connection)
            except (OSError, ValueError, RuntimeError, sqlite3.Error):
                # Broken shared corrections leave every publication unreadable, not absent.
                return ProposalHistoryRead((), ids)
            for proposal_id in ids:
                try:
                    proposals.append(_project(connection, proposal_id, records))
                except (OSError, ValueError, RuntimeError, sqlite3.Error):
                    # One unreadable publication must stay visible as missing, not erase its peers.
                    unavailable.append(proposal_id)
            proposals.sort(key=lambda row: (row.published_at, row.proposal_id), reverse=True)
            return ProposalHistoryRead(tuple(proposals), tuple(unavailable))


def _project(
    connection: sqlite3.Connection, proposal_id: str, records: ProposalRecordReader
) -> SavedProposalView:
    proposal, pairs = load_proposal_with_assessments(connection, proposal_id)
    view = project_saved_proposal(proposal, pairs)
    view.corrections, view.human_reports = records.read(proposal)
    market, charts = saved_proposal_report_context(connection, proposal, pairs)
    view.saved_market_context = SavedMarketContextView.model_validate(market)
    for item in view.items:
        assessment = item.assessment
        if assessment is not None and assessment.assessment_id in charts:
            assessment.estimated_model_chart = EstimatedModelChartView.model_validate(
                charts[assessment.assessment_id]
            )
    return view
