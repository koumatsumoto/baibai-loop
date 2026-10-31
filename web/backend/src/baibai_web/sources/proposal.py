"""正式保存提案を単一read-only transactionで読む。市場・台帳の現在値は取得しない。"""

import json
from contextlib import closing
from pathlib import Path

from baibai_engine.investment_judgment.stock_assessment.store import require_assessment_ref
from baibai_engine.investment_judgment.trade_proposal.query import (
    load_latest_investment_proposal,
    proposal_records,
)
from baibai_engine.storage.sqlite.application.read import connect_read_only
from baibai_web.readmodel.builders.proposal import project_saved_proposal
from baibai_web.readmodel.proposal_models import SavedProposalView


class DbProposalSource:
    def __init__(self, db_path: Path) -> None:
        self._path = db_path

    def latest(self) -> SavedProposalView | None:
        with closing(connect_read_only(self._path)) as connection:
            connection.execute("BEGIN")
            proposal = load_latest_investment_proposal(connection)
            if proposal is None:
                return None
            references = [
                item.assessment_ref for item in proposal.items if item.assessment_ref is not None
            ]
            references.extend(
                item.position.original_assessment_ref
                for item in proposal.items
                if item.action in {"hold", "sell"}
                and item.position.original_assessment_ref is not None
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
