"""財務の発見レーンと、Researchに固定した過去の技術・財務観測の保存型。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue

type ScreeningLane = Literal[
    "growth_research", "input_review", "alternative_financial_research", "no_current_hypothesis"
]


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
        "technical-financial-discovery-v13",
        "technical-financial-discovery-v14",
        "technical-financial-discovery-v15",
        "technical-financial-discovery-v16",
        "technical-financial-discovery-v17",
        "technical-financial-discovery-v18",
        "technical-financial-discovery-v19",
        "technical-financial-discovery-v20",
        "technical-financial-discovery-v21",
        "technical-financial-discovery-v22",
        "technical-financial-discovery-v23",
        "technical-financial-discovery-v24",
        "technical-financial-discovery-v25",
        "technical-financial-discovery-v26",
    ] = "technical-financial-discovery-v26"
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
    related_subject_context: Annotated[
        dict[str, JsonValue] | None, Field(exclude_if=lambda value: value is None)
    ] = None


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
