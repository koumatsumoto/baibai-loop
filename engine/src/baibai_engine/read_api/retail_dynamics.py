"""Query-only broker observations for Web and developer tools."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any

from baibai_engine.market.retail_dynamics.analysis import analyze
from baibai_engine.market.retail_dynamics.reader import read_observations


def retail_dynamics_payload(
    sqlite_path: Path, *, ticker: str, cutoff: datetime, evaluated_at: datetime
) -> dict[str, Any]:
    """Return received-time inputs and comparison reasons at one explicit cutoff."""
    history = read_observations(sqlite_path, ticker, cutoff)
    return analyze(history, evaluated_at=evaluated_at).model_dump(mode="json")
