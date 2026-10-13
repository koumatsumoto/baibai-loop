"""候補 membership に依存しない、保存済み機械評価の読取 use case。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from pathlib import Path

from baibai_engine.foundation.sqlite_read import read_store_if_written
from baibai_engine.screening.run_store import ScreeningRunReader
from baibai_engine.valuation.metrics.history import VALUATION_HISTORY_SESSIONS
from baibai_engine.valuation.query import (
    MachineValuation,
    anchor_gap_pct,
    machine_valuation_from_analysis,
)

__all__ = [
    "VALUATION_HISTORY_SESSIONS",
    "StoredMachineValuation",
    "anchor_gap_pct",
    "machine_valuation_from_analysis",
    "stored_machine_valuation",
]


@dataclass(frozen=True, slots=True)
class StoredMachineValuation:
    run_revision_id: str
    as_of: date
    valuation: MachineValuation | None


def stored_machine_valuation(
    path: Path, *, ticker: str, as_of: date, run_revision_id: str | None = None
) -> StoredMachineValuation | None:
    """Select latest run at/before cutoff, then the ticker; never fall back across scope.

    No run means None. A selected run without the ticker has valuation=None;
    a stored analysis without estimates has missing coordinates. Fixed run
    identities are checked against the same cutoff.
    """
    reader = ScreeningRunReader(path)
    run = read_store_if_written(
        path,
        lambda: reader.valuation_run(as_of=as_of, run_revision_id=run_revision_id),
    )
    if run is None:
        return None
    analysis = next((row for row in run.security_analyses if row.get("ticker") == ticker), None)
    return StoredMachineValuation(
        run.run_revision_id,
        date.fromisoformat(run.as_of_date),
        None if analysis is None else machine_valuation_from_analysis(analysis),
    )
