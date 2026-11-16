"""現行の事業座標と旧経済政策の保存型。過去の順位は再計算しない。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, FiniteFloat, JsonValue

PathId = Literal[
    "earning_power", "business_expansion", "operating_recovery", "capital_productivity"
]
CoordinateValue = Annotated[FiniteFloat, Field(strict=True)]
Percentile = Annotated[CoordinateValue, Field(gt=0, lt=1)]


class EconomicPathPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    method: Literal[
        "best-economic-path-v1",
        "best-economic-path-v2",
        "best-economic-path-v3",
        "best-economic-path-v4",
    ] = "best-economic-path-v4"
    candidate_limit: Annotated[int, Field(ge=0, le=36)] = 36


@dataclass(frozen=True, slots=True)
class PathCoordinates:
    path_id: PathId
    variant: str
    price_value: CoordinateValue | None
    business_value: CoordinateValue | None
    price_group: str
    business_group: str
    eligible: bool
    evidence: dict[str, JsonValue]


@dataclass(frozen=True, slots=True)
class CompanyCoordinates:
    ticker: str
    name: str | None
    sector: str | None
    paths: tuple[PathCoordinates, ...]
    context: dict[str, JsonValue]
    model_revisions: dict[str, str]
    not_generated: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class PathNormalization:
    """Hの同一経路内順位。母集団は二軸参照群ではなく有効企業群。"""

    percentile: Percentile
    population: Annotated[int, Field(strict=True, gt=0)]
    midrank: CoordinateValue


@dataclass(frozen=True, slots=True)
class RankedPath:
    path_id: PathId
    variant: str
    price_value: CoordinateValue
    business_value: CoordinateValue
    price_group: str
    business_group: str
    price_percentile: Percentile
    business_percentile: Percentile
    price_population: Annotated[int, Field(strict=True, gt=0)]
    business_population: Annotated[int, Field(strict=True, gt=0)]
    score: Percentile
    evidence: dict[str, JsonValue]
    normalization: Annotated[
        PathNormalization | None, Field(exclude_if=lambda value: value is None)
    ] = None


@dataclass(frozen=True, slots=True)
class CandidateCard:
    ticker: str
    name: str | None
    sector: str | None
    rank: Annotated[int, Field(strict=True, gt=0)]
    priority: Percentile
    strongest_path: PathId
    paths: tuple[RankedPath, ...]
    context: dict[str, JsonValue]
    model_revisions: dict[str, str]
    not_generated: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class EconomicPathSelection:
    policy: EconomicPathPolicy
    eligible_count: Annotated[int, Field(strict=True, ge=0)]
    cards: tuple[CandidateCard, ...]

    @property
    def tickers(self) -> tuple[str, ...]:
        """保存対象集合。優先順位はcard.rankで明示する。"""
        return tuple(sorted(card.ticker for card in self.cards))
