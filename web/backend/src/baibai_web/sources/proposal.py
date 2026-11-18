"""正式保存提案を単一read-only transactionで読む。市場・台帳の現在値は取得しない。"""

import sqlite3
from contextlib import closing
from pathlib import Path

from baibai_engine.investment_judgment.trade_proposal.query import (
    latest_investment_proposal_id,
    load_proposal_with_assessments,
    published_proposal_ids,
    read_proposal_records,
    saved_proposal_market_context,
)
from baibai_engine.storage.sqlite.application.read import connect_read_only
from baibai_web.readmodel.builders.proposal import project_saved_proposal
from baibai_web.readmodel.proposal_models import SavedMarketContextView, SavedProposalView
from baibai_web.sources.protocols import ProposalHistoryRead


class DbProposalSource:
    def __init__(self, db_path: Path) -> None:
        self._path = db_path

    def latest(self) -> SavedProposalView | None:
        with closing(connect_read_only(self._path)) as connection:
            connection.execute("BEGIN")
            proposal_id = latest_investment_proposal_id(connection)
            return None if proposal_id is None else _project(connection, proposal_id)

    def history(self) -> ProposalHistoryRead:
        with closing(connect_read_only(self._path)) as connection:
            connection.execute("BEGIN")
            proposals = []
            unavailable = []
            for proposal_id in published_proposal_ids(connection):
                try:
                    proposals.append(_project(connection, proposal_id))
                except (OSError, ValueError, RuntimeError, sqlite3.Error):
                    # One unreadable publication must stay visible as missing, not erase its peers.
                    unavailable.append(proposal_id)
            proposals.sort(key=lambda row: (row.published_at, row.proposal_id), reverse=True)
            return ProposalHistoryRead(tuple(proposals), tuple(unavailable))


def _project(connection: sqlite3.Connection, proposal_id: str) -> SavedProposalView:
    proposal, pairs = load_proposal_with_assessments(connection, proposal_id)
    view = project_saved_proposal(proposal, pairs)
    view.corrections, view.human_reports = read_proposal_records(connection, proposal)
    view.saved_market_context = SavedMarketContextView.model_validate(
        saved_proposal_market_context(connection, proposal)
    )
    return view
