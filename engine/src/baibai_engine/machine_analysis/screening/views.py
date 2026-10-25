"""モデル観測から独立した有限候補を選ぶ。保存・取得・投資判断は行わない。"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, FiniteFloat


class ScreeningCoordinates(BaseModel):
    """政策専用の投影。元の期間・出所・欠測理由は生成モデル側に保持する。"""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    ticker: Annotated[str, Field(pattern=r"^[0-9A-Z]{4}$")]
    earnings_yield: FiniteFloat | None
    price_momentum: Annotated[FiniteFloat, Field(ge=-1)] | None


class TwoViewPolicy(BaseModel):
    """観測を追加しても暗黙に変わらない、明示的な選定政策。"""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    method: Literal["earnings-price-views-v1"] = "earnings-price-views-v1"
    earnings_limit: Annotated[int, Field(ge=0)]
    combined_limit: Annotated[int, Field(ge=0)]
    earnings_scale: Annotated[FiniteFloat, Field(gt=0)] = 0.10
    momentum_scale: Annotated[FiniteFloat, Field(gt=0)] = 0.50
    earnings_weight: Annotated[FiniteFloat, Field(ge=0, le=1)] = 0.60

    @property
    def momentum_weight(self) -> float:
        return 1 - self.earnings_weight


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


def select_views(
    coordinates: Sequence[ScreeningCoordinates], *, policy: TwoViewPolicy
) -> ViewSelection:
    """各viewを独立に打ち切る。欠測補完・重複加点・自動補充はしない。"""
    if len({row.ticker for row in coordinates}) != len(coordinates):
        raise ValueError("screening coordinates must contain one row per ticker")
    earnings: list[tuple[str, float]] = []
    combined: list[tuple[str, float]] = []
    for row in coordinates:
        value = row.earnings_yield
        if value is None:
            continue
        earnings.append((row.ticker, value))
        momentum = row.price_momentum
        if momentum is not None:
            score = policy.earnings_weight * value / (
                policy.earnings_scale + abs(value)
            ) + policy.momentum_weight * momentum / (policy.momentum_scale + abs(momentum))
            combined.append((row.ticker, score))
    return ViewSelection(
        policy=policy,
        views=(
            _rank("earnings-price", earnings, policy.earnings_limit),
            _rank("earnings-price-response", combined, policy.combined_limit),
        ),
    )


def _rank(
    view_id: Literal["earnings-price", "earnings-price-response"],
    values: list[tuple[str, float]],
    limit: int,
) -> CandidateView:
    ordered = sorted(values, key=lambda item: (-item[1], item[0]))
    return CandidateView(
        view_id=view_id,
        limit=limit,
        eligible_count=len(values),
        companies=tuple(
            RankedCompany(ticker=ticker, rank=rank, ranking_value=value)
            for rank, (ticker, value) in enumerate(ordered[:limit], start=1)
        ),
    )
