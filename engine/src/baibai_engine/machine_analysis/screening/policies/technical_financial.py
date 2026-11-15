"""技術・市場・財務の発見根拠と、投資順位でない有限表示順を保存する。"""

from __future__ import annotations

from collections import deque
from collections.abc import Sequence
from dataclasses import dataclass
from hashlib import sha256
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

    method: Literal[
        "technical-financial-discovery-v1",
        "technical-financial-discovery-v2",
        "technical-financial-discovery-v3",
        "technical-financial-discovery-v4",
        "technical-financial-discovery-v5",
        "technical-financial-discovery-v6",
        "technical-financial-discovery-v7",
        "technical-financial-discovery-v8",
        "technical-financial-discovery-v9",
        "technical-financial-discovery-v10",
        "technical-financial-discovery-v11",
        "technical-financial-discovery-v12",
    ] = "technical-financial-discovery-v12"
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
    *,
    growth_state: str,
    technical_match: bool,
    financial_path_match: bool,
    financial_input_review: bool = False,
) -> ScreeningLane:
    if growth_state == "research_candidate":
        return "growth_research"
    if growth_state == "input_unresolved" and (
        technical_match or financial_path_match or financial_input_review
    ):
        return "input_review"
    if growth_state == "not_selected" and financial_path_match:
        return "alternative_financial_research"
    if growth_state == "not_selected" and financial_input_review:
        return "input_review"
    return "no_current_hypothesis"


def discovery_display_order(rows: Sequence[TechnicalFinancialRow]) -> tuple[str, ...]:
    """lane位置を保ち、コード番号の大小を調査価値の代理にしない。"""
    order = _route_display_order(rows)
    for lane in RESEARCH_LANES:
        lane_rows = tuple(row for row in rows if row.screening_lane == lane)
        lane_tickers = {row.ticker for row in lane_rows}
        lane_order = iter(_route_display_order(lane_rows, company_hash=True))
        order = tuple(next(lane_order) if ticker in lane_tickers else ticker for ticker in order)
    return order


def _company_attention_key(row: TechnicalFinancialRow) -> tuple[bytes, str]:
    # 経路ごとの独立したくじにせず、正準tickerに一つの再読可能な注意順を与える。
    digest = sha256(f"baibai-loop/attention-control/v1\0{row.ticker}".encode()).digest()
    return digest, row.ticker


def _route_display_order(
    rows: Sequence[TechnicalFinancialRow], *, company_hash: bool = False
) -> tuple[str, ...]:
    queues: dict[str, deque[str]] = {}
    ordered = sorted(
        rows,
        key=_company_attention_key if company_hash else lambda row: (b"", row.ticker),
    )
    for row in ordered:
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
        reviews = row.financial_paths.get("input_review_reasons", [])
        assert isinstance(reviews, list)
        for review in reviews:
            assert isinstance(review, dict)
            route = review["path_id"]
            assert isinstance(route, str)
            queues.setdefault(f"financial_input_review:{route}", deque()).append(row.ticker)
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
