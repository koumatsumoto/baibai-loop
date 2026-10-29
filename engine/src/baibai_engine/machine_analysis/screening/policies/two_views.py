"""保存済みの旧Y/YM政策結果を読む契約。新規選定は行わない。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, FiniteFloat


class TwoViewPolicy(BaseModel):
    """旧結果のidentityを保つため、保存した政策fieldをそのまま読む。"""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    method: Literal["earnings-price-views-v1"] = "earnings-price-views-v1"
    earnings_limit: Annotated[int, Field(ge=0)]
    combined_limit: Annotated[int, Field(ge=0)]
    earnings_scale: Annotated[FiniteFloat, Field(gt=0)] = 0.10
    momentum_scale: Annotated[FiniteFloat, Field(gt=0)] = 0.50
    earnings_weight: Annotated[FiniteFloat, Field(ge=0, le=1)] = 0.60


@dataclass(frozen=True, slots=True)
class RankedCompany:
    ticker: str
    rank: int
    ranking_value: float


@dataclass(frozen=True, slots=True)
class CandidateView:
    view_id: Literal["earnings-price", "earnings-price-response"]
    limit: int
    eligible_count: int
    companies: tuple[RankedCompany, ...]


@dataclass(frozen=True, slots=True)
class ViewSelection:
    policy: TwoViewPolicy
    views: tuple[CandidateView, ...]

    @property
    def tickers(self) -> tuple[str, ...]:
        """企業単位の和集合。証券コード順であり、全体の投資順位ではない。"""
        return tuple(sorted({row.ticker for view in self.views for row in view.companies}))
