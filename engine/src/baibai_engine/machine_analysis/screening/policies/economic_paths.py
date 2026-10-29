"""価格と事業の二座標から調査優先度を選ぶ。期待収益や投資採否ではない。"""

from __future__ import annotations

from bisect import bisect_left, bisect_right
from collections.abc import Sequence
from dataclasses import dataclass
from math import isfinite
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, FiniteFloat, JsonValue

PathId = Literal[
    "earning_power", "business_expansion", "operating_recovery", "capital_productivity"
]
CoordinateValue = Annotated[FiniteFloat, Field(strict=True)]
Percentile = Annotated[CoordinateValue, Field(gt=0, lt=1)]


class EconomicPathPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    method: Literal["best-economic-path-v1"] = "best-economic-path-v1"
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


def select_economic_paths(
    companies: Sequence[CompanyCoordinates], *, policy: EconomicPathPolicy
) -> EconomicPathSelection:
    """各軸の母集団を先に固定する。欠測の片軸補完や経路の投票はしない。"""
    if len({row.ticker for row in companies}) != len(companies):
        raise ValueError("economic screening requires one row per ticker")
    # variantは適用期間を先に選ぶためのもの。座標の期間・basisはgroupが定める。
    # 四半期・年次に共通の年次価格座標は、一社を二回数えない。
    observations: dict[tuple[str, str, str], dict[str, float]] = {}
    for row in companies:
        if len({(path.path_id, path.variant) for path in row.paths}) != len(row.paths):
            raise ValueError("economic path variants must be unique per company")
        active = [path.path_id for path in row.paths if path.eligible]
        if len(set(active)) != len(active):
            raise ValueError("select the period variant before ranking each economic path")
        for path in row.paths:
            for axis, group, value in (
                ("price", path.price_group, path.price_value),
                ("business", path.business_group, path.business_value),
            ):
                if value is not None:
                    _validate_coordinate(value)
                    population = observations.setdefault((path.path_id, axis, group), {})
                    if row.ticker in population and population[row.ticker] != value:
                        raise ValueError("a reference coordinate must have one value per company")
                    population[row.ticker] = value
    populations = {key: sorted(values.values()) for key, values in observations.items()}
    ranked: list[tuple[CompanyCoordinates, tuple[RankedPath, ...]]] = []
    for row in companies:
        paths: list[RankedPath] = []
        for path in row.paths:
            if not path.eligible or path.price_value is None or path.business_value is None:
                continue
            prices = populations[(path.path_id, "price", path.price_group)]
            business = populations[(path.path_id, "business", path.business_group)]
            price_percentile = _midrank(prices, path.price_value)
            business_percentile = _midrank(business, path.business_value)
            paths.append(
                RankedPath(
                    path_id=path.path_id,
                    variant=path.variant,
                    price_value=path.price_value,
                    business_value=path.business_value,
                    price_group=path.price_group,
                    business_group=path.business_group,
                    price_percentile=price_percentile,
                    business_percentile=business_percentile,
                    price_population=len(prices),
                    business_population=len(business),
                    score=2
                    * price_percentile
                    * business_percentile
                    / (price_percentile + business_percentile),
                    evidence=path.evidence,
                )
            )
        if paths:
            ranked.append((row, tuple(sorted(paths, key=lambda item: (-item.score, item.path_id)))))
    ranked.sort(key=lambda item: (-item[1][0].score, item[0].ticker))
    return EconomicPathSelection(
        policy=policy,
        eligible_count=len(ranked),
        cards=tuple(
            CandidateCard(
                ticker=row.ticker,
                name=row.name,
                sector=row.sector,
                rank=rank,
                priority=paths[0].score,
                strongest_path=paths[0].path_id,
                paths=paths,
                context=row.context,
                model_revisions=row.model_revisions,
                not_generated=row.not_generated,
            )
            for rank, (row, paths) in enumerate(ranked[: policy.candidate_limit], start=1)
        ),
    )


def _validate_coordinate(value: object) -> None:
    if not isinstance(value, (int, float)) or isinstance(value, bool) or not isfinite(value):
        raise ValueError("economic coordinates must be finite or missing")


def _midrank(values: list[float], value: float) -> float:
    return (bisect_left(values, value) + bisect_right(values, value)) / (2 * len(values))
