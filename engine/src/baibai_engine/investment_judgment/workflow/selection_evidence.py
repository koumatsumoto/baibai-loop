"""公開時の原canonicalと現在の運用入力を、明示されたhashで区別する。"""

import hashlib
import json
from contextlib import closing
from pathlib import Path

from baibai_engine.investment_judgment.trade_proposal.query import load_investment_proposal
from baibai_engine.investment_judgment.workflow.selection import parse_selection, selection_sha256
from baibai_engine.primitives.json import canonical_json
from baibai_engine.storage.sqlite.application.read import connect_read_only


def read_selection_evidence(
    *,
    operation_id: str,
    sha256: str,
    proposal_id: str | None = None,
    db_path: Path | None = None,
) -> dict[str, str]:
    """履歴はopaqueな原文を返すだけで、旧IDのモデル解釈やhashの付替えをしない。"""
    with closing(connect_read_only(db_path)) as connection:
        connection.execute("BEGIN")
        row = connection.execute(
            "SELECT status,payload FROM operation_session WHERE operation_id=?", (operation_id,)
        ).fetchone()
        if row is None:
            raise ValueError("selection operation is unavailable")
        artifacts = [
            item
            for item in json.loads(row[1])["artifacts"]
            if item.get("kind") == "research_selection"
        ]
        if len(artifacts) != 1:
            raise ValueError("one fixed selection is required")
        selection = parse_selection(artifacts[0])
        current = selection_sha256(selection)
        receipt = connection.execute(
            "SELECT migration_id,published_selection_sha256,operational_selection_sha256,"
            "published_selection_canonical FROM selection_identity_migration WHERE operation_id=?",
            (operation_id,),
        ).fetchone()
        if receipt is not None and (
            row[0] != "completed"
            or receipt[0] != "company-financials-id-v1"
            or receipt[2] != current
            or hashlib.sha256(receipt[3].encode()).hexdigest() != receipt[1]
        ):
            raise ValueError("selection migration evidence differs from its exact inputs")
        if proposal_id is not None:
            proposal = load_investment_proposal(connection, proposal_id)
            if (
                proposal.research_scope.operation_id != operation_id
                or proposal.research_scope.selection_sha256 != sha256
            ):
                raise ValueError("proposal scope differs from the requested selection evidence")
        if sha256 == current:
            return {
                "operation_id": operation_id,
                "role": "operational",
                "selection_sha256": current,
                "operational_selection_sha256": current,
                "canonical_json": canonical_json(selection.model_dump(mode="json")),
            }
        if receipt is None or receipt[1] != sha256 or proposal_id is None:
            raise ValueError("historical evidence requires its exact published proposal and hash")
        return {
            "operation_id": operation_id,
            "role": "as_published",
            "selection_sha256": str(receipt[1]),
            "operational_selection_sha256": current,
            "canonical_json": str(receipt[3]),
        }
