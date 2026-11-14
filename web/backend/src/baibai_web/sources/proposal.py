"""正式保存提案を単一read-only transactionで読む。市場・台帳の現在値は取得しない。"""

import json
import sqlite3
from contextlib import closing
from pathlib import Path

from baibai_engine.investment_judgment.stock_assessment.store import require_assessment_ref
from baibai_engine.investment_judgment.trade_proposal.contracts import TradeProposal
from baibai_engine.investment_judgment.trade_proposal.query import (
    load_investment_proposal,
    load_latest_investment_proposal,
    proposal_records,
    published_proposal_ids,
)
from baibai_engine.storage.sqlite.application.read import connect_read_only
from baibai_web.readmodel.builders.proposal import project_saved_proposal
from baibai_web.readmodel.proposal_models import SavedProposalView
from baibai_web.sources.protocols import ProposalHistoryRead


class DbProposalSource:
    def __init__(self, db_path: Path) -> None:
        self._path = db_path

    def latest(self) -> SavedProposalView | None:
        with closing(connect_read_only(self._path)) as connection:
            connection.execute("BEGIN")
            proposal = load_latest_investment_proposal(connection)
            if proposal is None:
                return None
            return _project(connection, proposal)

    def history(self) -> ProposalHistoryRead:
        with closing(connect_read_only(self._path)) as connection:
            connection.execute("BEGIN")
            proposals = []
            unavailable = []
            for proposal_id in published_proposal_ids(connection):
                try:
                    proposal = load_investment_proposal(connection, proposal_id)
                    proposals.append(_project(connection, proposal))
                except (OSError, ValueError, RuntimeError, sqlite3.Error):
                    # One unreadable publication must stay visible as missing, not erase its peers.
                    unavailable.append(proposal_id)
            proposals.sort(key=lambda row: (row.published_at, row.proposal_id), reverse=True)
            return ProposalHistoryRead(tuple(proposals), tuple(unavailable))


def _project(connection: sqlite3.Connection, proposal: TradeProposal) -> SavedProposalView:
    references = [item.assessment_ref for item in proposal.items if item.assessment_ref is not None]
    references.extend(
        item.position.original_assessment_ref
        for item in proposal.items
        if item.action in {"hold", "sell"} and item.position.original_assessment_ref is not None
    )
    pairs = {
        reference.assessment_id: require_assessment_ref(connection, reference)
        for reference in references
    }
    view = project_saved_proposal(proposal, pairs)
    return type(view).model_validate_json(
        json.dumps(
            {
                **view.model_dump(mode="json"),
                **proposal_records(connection, proposal.proposal_id),
            }
        )
    )
