"""保存済み配分判断のidentityを読み、現在の適格性を再判定しない。"""

from __future__ import annotations

import json
from contextlib import closing
from pathlib import Path

from baibai_engine.research.capital_allocation import CapitalAllocationConflictError
from baibai_engine.storage.application.paths import database_path
from baibai_engine.storage.application.read import connect_read_only


def historical_allocated_ticker(assessment_id: str, *, db_path: Path | None = None) -> str:
    """Resolve recorded order identity without replaying past investment eligibility."""
    path = database_path(db_path)
    if not path.is_file():
        raise CapitalAllocationConflictError(f"application database is unavailable: {path}")
    with closing(connect_read_only(path)) as connection:
        row = connection.execute(
            "SELECT result, payload FROM capital_allocation_assessment "
            "WHERE capital_allocation_assessment_id = ?",
            (assessment_id,),
        ).fetchone()
    if row is None or row[0] != "allocate":
        raise CapitalAllocationConflictError("assessment is not an allocate decision")
    payload = json.loads(str(row[1]))
    if not isinstance(payload, dict) or payload.get("result") != "allocate":
        raise CapitalAllocationConflictError("invalid historical allocation identity")
    alternatives = payload.get("alternatives")
    if not isinstance(alternatives, list):
        raise CapitalAllocationConflictError("missing historical alternatives")
    allocated = [
        item
        for item in alternatives
        if isinstance(item, dict) and item.get("disposition") == "allocate"
    ]
    if len(allocated) != 1 or not isinstance(allocated[0].get("ticker"), str):
        raise CapitalAllocationConflictError("ambiguous historical allocation identity")
    return str(allocated[0]["ticker"])
