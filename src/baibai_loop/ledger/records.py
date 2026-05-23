from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Literal


@dataclass(frozen=True, slots=True)
class Tracking:
    mode: Literal["post_approval", "re_examination", "none"]
    plus_15bd: float | None = None
    plus_30bd: float | None = None


@dataclass(frozen=True, slots=True)
class DecisionRegisterRecord:
    decision_event_id: str
    event_kind: Literal["decision", "correction"]
    decision_scope: Literal["research_memo", "trade_execution"]
    ticker: str
    name: str
    trade_execution_state: str
    provenance: Literal["regenerated", "manual"] = "regenerated"
    research_decision: dict[str, object] | None = None
    order_intent: dict[str, object] | None = None
    candidate_ref: dict[str, object] | None = None
    research_ref: str | None = None
    trade_ref: str | None = None
    decision_event_at: str | None = None
    playbook_id: str | None = None
    playbook_ref: dict[str, object] | None = None
    macro_context_ref: str | None = None
    macro_context_fit: dict[str, object] | None = None
    macro_context_decision_effect: str | None = None
    baseline_price: float | None = None
    market_cap_oku: float | None = None
    avg_turnover_oku: float | None = None
    independent_evidence_count: int | None = None
    conviction_tier: str | None = None
    tracking: Tracking | None = None

    def to_json(self) -> dict[str, object]:
        return {key: value for key, value in asdict(self).items() if value is not None}
