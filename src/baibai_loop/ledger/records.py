from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Literal


@dataclass(frozen=True, slots=True)
class Tracking:
    plus_15bd: float | None
    plus_30bd: float | None


@dataclass(frozen=True, slots=True)
class PaperLedgerRecord:
    ledger_id: str
    ticker: str
    name: str
    decision: Literal["accepted", "pending"]
    playbook: str
    candidates_ref: str
    research_ref: str
    asof_date: str
    decision_date: str
    baseline_price: float | None
    market_cap_oku: float | None
    avg_turnover_oku: float | None
    signal_count: int
    macro_gate: str
    adv_participation_pct: float | None
    adjustment_applied: bool
    tracking: Tracking

    def to_json(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class SkippedLedgerRecord:
    ledger_id: str
    ticker: str
    name: str
    decision: Literal["skipped"]
    playbook: str
    candidates_ref: str
    research_ref: str | None
    asof_date: str
    decision_date: str
    baseline_price: float | None
    market_cap_oku: float | None
    avg_turnover_oku: float | None
    signal_count: int
    macro_gate: str | None
    adv_participation_pct: float | None
    adjustment_applied: bool
    tracking: Tracking

    def to_json(self) -> dict[str, object]:
        return asdict(self)
