"""技術・市場・財務の発見根拠と、投資順位でない有限表示順を保存する。"""

from __future__ import annotations

from collections import deque
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue

type ResearchLane = Literal["growth_research", "input_review", "alternative_financial_research"]
RESEARCH_LANES: tuple[ResearchLane, ...] = (
    "growth_research",
    "input_review",
    "alternative_financial_research",
)
type ScreeningLane = ResearchLane | Literal["no_current_hypothesis"]


class TechnicalFinancialPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    method: Literal["technical-financial-discovery-v1"] = "technical-financial-discovery-v1"
    page_size: Annotated[int, Field(ge=1, le=36)] = 36


@dataclass(frozen=True, slots=True)
class TechnicalFinancialRow:
    ticker: str
    name: str | None
    sector: str | None
    technical: dict[str, JsonValue]
    financial_gate: dict[str, JsonValue]
    financial_paths: dict[str, JsonValue]
    screening_lane: ScreeningLane


@dataclass(frozen=True, slots=True)
class TechnicalFinancialSelection:
    policy: TechnicalFinancialPolicy
    technical_policy_id: str
    financial_policy_id: str
    rows: tuple[TechnicalFinancialRow, ...]
    display_order: tuple[str, ...]
    summary: dict[str, JsonValue]

    @property
    def tickers(self) -> tuple[str, ...]:
        """G仮説合致と予備調査を含む閲覧集合。Research採択・買付適格性ではない。"""
        return tuple(sorted(self.display_order))

    def lane_tickers(self, lane: ResearchLane) -> tuple[str, ...]:
        if lane not in RESEARCH_LANES:
            raise ValueError("unknown screening lane")
        members = {row.ticker for row in self.rows if row.screening_lane == lane}
        return tuple(ticker for ticker in self.display_order if ticker in members)


def financial_screening_lane(
    *, growth_state: str, technical_match: bool, financial_path_match: bool
) -> ScreeningLane:
    if growth_state == "research_candidate":
        return "growth_research"
    if growth_state == "input_unresolved" and (technical_match or financial_path_match):
        return "input_review"
    if growth_state == "not_selected" and financial_path_match:
        return "alternative_financial_research"
    return "no_current_hypothesis"


def discovery_display_order(rows: Sequence[TechnicalFinancialRow]) -> tuple[str, ...]:
    """経路を巡回する注意順。コード順は同順位の固定化であり優位の推定でない。"""
    queues: dict[str, deque[str]] = {}
    for row in sorted(rows, key=lambda row: row.ticker):
        if row.screening_lane == "no_current_hypothesis":
            continue
        technical_match = row.technical["state"] == "matched"
        sources = (
            ("growth", row.financial_gate.get("matched_routes", [])),
            ("financial", row.financial_paths["matched_routes"]),
            ("technical", row.technical["matched_routes"] if technical_match else []),
        )
        for prefix, routes in sources:
            assert isinstance(routes, list)
            for route in routes:
                assert isinstance(route, str)
                queues.setdefault(f"{prefix}:{route}", deque()).append(row.ticker)
    order: list[str] = []
    selected: set[str] = set()
    while any(queues.values()):
        for route in sorted(queues):
            queue = queues[route]
            while queue and queue[0] in selected:
                queue.popleft()
            if queue:
                ticker = queue.popleft()
                order.append(ticker)
                selected.add(ticker)
    return tuple(order)
