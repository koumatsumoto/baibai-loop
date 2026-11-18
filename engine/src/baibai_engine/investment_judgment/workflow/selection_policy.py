"""深掘り件数の実行policy。構造型と保存済みpolicyを分ける。"""

from typing import Literal

from pydantic import BaseModel, ConfigDict, PositiveInt


class ResearchCapacity(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    policy_id: Literal["research-capacity-v1"] = "research-capacity-v1"
    max_deep_research_targets: PositiveInt = 3


CURRENT_RESEARCH_CAPACITY = ResearchCapacity()


def research_capacity(scope: str, target_count: int) -> ResearchCapacity:
    # 保有の範囲は台帳で入場時に検証する。既保有の全体見直しを新規調査枠で分断しない。
    if (
        scope == "review_positions"
        and target_count > CURRENT_RESEARCH_CAPACITY.max_deep_research_targets
    ):
        return ResearchCapacity(max_deep_research_targets=target_count)
    return CURRENT_RESEARCH_CAPACITY


def validate_capacity(target_count: int, capacity: ResearchCapacity) -> None:
    if not 0 <= target_count <= capacity.max_deep_research_targets:
        raise ValueError("selected targets exceed the saved research capacity")
