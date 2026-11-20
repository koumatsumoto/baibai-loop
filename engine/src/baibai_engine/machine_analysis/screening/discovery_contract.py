"""全普通株の薄い同時点観測。経路や表示枠を投資判断にしない。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from typing import Annotated, Final, Literal, TypedDict, TypeIs

from pydantic import BaseModel, ConfigDict, Field, JsonValue, StrictInt, TypeAdapter, with_config

from baibai_engine.machine_analysis.generation.contracts import GenerationMetadata
from baibai_engine.machine_analysis.observations.prices.price_observation import (
    ExpectedPriceBar,
    NativePriceSession,
)

type DiscoveryMethod = Literal["opportunity-discovery-v1", "opportunity-discovery-v2"]
DISCOVERY_V2: Final = "opportunity-discovery-v2"
DISCOVERY_METHOD: Final = "opportunity-discovery-v1"
DISCOVERY_METHODS: Final = (DISCOVERY_METHOD, DISCOVERY_V2)


class DiscoveryPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    method: Literal["opportunity-discovery-v1"] = DISCOVERY_METHOD


class DiscoveryPolicyV2(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    method: Literal["opportunity-discovery-v2"]


_OBSERVATION = ConfigDict(strict=True, allow_inf_nan=False)
type ObservedNumber = StrictInt | Annotated[float, Field(strict=True, allow_inf_nan=False)]
type EventCount = Annotated[int, Field(strict=True, ge=0)]


@with_config(_OBSERVATION)
class MatchedPaths(TypedDict):
    financial: list[str]
    growth: list[str]


@with_config(_OBSERVATION)
class EconomicCoordinate(TypedDict):
    path_id: str
    variant: str
    price_group: str
    business_group: str
    price_value: ObservedNumber | None
    business_value: ObservedNumber | None


@with_config(_OBSERVATION)
class CapitalActionCounts(TypedDict):
    observations: EventCount | None
    share_basis_adjustments: EventCount | None


@with_config(_OBSERVATION)
class BookEquity(TypedDict, total=False):
    amount_yen: ObservedNumber | None
    method: str | None
    measurement_date: str | None
    statement_basis: str | None
    disclosed_at: str | None


@with_config(_OBSERVATION)
class DiscoveryFacts(TypedDict):
    ticker: str
    models_recorded: bool
    name: str | None
    sector: str | None
    screening_lane: str
    technical_state: str
    financial_state: str
    matched_paths: MatchedPaths
    missing: list[str]
    coordinates: list[EconomicCoordinate]
    book_equity: BookEquity | None
    capital_action: CapitalActionCounts | None


class DiscoveryRow(DiscoveryFacts):
    price: ObservedNumber | None
    price_date: str | None
    expected_price_date: str | None
    basis: str | None
    price_ready: bool


@with_config(ConfigDict(strict=True, extra="forbid", allow_inf_nan=False))
class DiscoveryRowV2(DiscoveryFacts):
    execution_price: ExpectedPriceBar


type IndexedRow = DiscoveryRow | DiscoveryRowV2


def is_session_row(row: IndexedRow) -> TypeIs[DiscoveryRowV2]:
    return "execution_price" in row


@dataclass(frozen=True, slots=True)
class DiscoveryIndexRead:
    status: Literal["no_publication", "unindexed_legacy", "indexed"]
    selection_id: str | None = None
    generation_id: str | None = None
    as_of: date | None = None
    evaluation_time: datetime | None = None
    published_at: datetime | None = None
    rows: tuple[IndexedRow, ...] = ()
    universe_tickers: tuple[str, ...] = ()
    price_session: NativePriceSession | None = None


@dataclass(frozen=True, slots=True)
class DiscoverySelection:
    policy: DiscoveryPolicy
    rows: tuple[DiscoveryRow, ...]
    summary: dict[str, JsonValue]

    @property
    def tickers(self) -> tuple[str, ...]:
        """旧レーン外・未観測も探索母集団。順位・買い適格集合ではない。"""
        return tuple(row["ticker"] for row in self.rows)


@dataclass(frozen=True, slots=True)
class DiscoverySelectionV2:
    policy: DiscoveryPolicyV2
    rows: tuple[DiscoveryRowV2, ...]
    summary: dict[str, JsonValue]
    price_session: NativePriceSession

    @property
    def tickers(self) -> tuple[str, ...]:
        return tuple(row["ticker"] for row in self.rows)


type IndexedSelection = DiscoverySelection | DiscoverySelectionV2


def discovery_publication_id(generation_id: str, method: DiscoveryMethod = DISCOVERY_METHOD) -> str:
    return f"selection-{generation_id}-{method}"


def discovery_method_for_id(selection_id: str, generation_id: str) -> DiscoveryMethod | None:
    # An unknown edition belongs to the discovery family; it is never an archived card.
    suffix = selection_id.removeprefix(f"selection-{generation_id}-")
    for method in DISCOVERY_METHODS:
        if selection_id == discovery_publication_id(generation_id, method):
            return method
    if suffix.startswith("opportunity-discovery-") or "-opportunity-discovery-" in suffix:
        raise ValueError("unknown discovery publication version or identity")
    return None


_ROW_CODECS = (TypeAdapter(tuple[DiscoveryRow, ...]), TypeAdapter(tuple[DiscoveryRowV2, ...]))


def require_publishable_rows(result: IndexedSelection) -> None:
    # TypedDict values can change between projection and publication.
    _ROW_CODECS[isinstance(result, DiscoverySelectionV2)].validate_python(result.rows, strict=True)


def planned_stock_tickers(metadata: GenerationMetadata) -> tuple[str, ...]:
    if metadata.subject_plan is None:
        raise ValueError("discovery V2 requires a fixed generation stock plan")
    return tuple(sorted(t for t, row in metadata.subject_plan.items() if row.scope == "stock"))


def validate_discovery_publication(
    *,
    selection_id: str,
    generation_id: str,
    result: IndexedSelection,
    universe_tickers: tuple[str, ...],
    metadata: GenerationMetadata | None = None,
) -> None:
    if discovery_method_for_id(selection_id, generation_id) != result.policy.method:
        raise ValueError("discovery publication identity differs from its generation or format")
    if result.tickers != universe_tickers or len(set(universe_tickers)) != len(universe_tickers):
        raise ValueError("discovery result differs from the exact stock universe")
    if isinstance(result, DiscoverySelectionV2):
        if metadata is None or metadata.generation_id != generation_id:
            raise ValueError("discovery V2 requires its exact generation header")
        if universe_tickers != planned_stock_tickers(metadata):
            raise ValueError("discovery universe differs from the saved stock plan")
        result.price_session.validate_cutoff(metadata.as_of, metadata.evaluation_time)
        for row in result.rows:
            bar = row["execution_price"]
            state, price = bar["day_state"], bar["raw_close_yen"]
            if (
                ((state is None or state == "no_trade") and price is not None)
                or (state == "priced" and (price is None or price <= 0))
                or (result.price_session.expected_date is None and state is not None)
            ):
                raise ValueError("recorded price differs from the expected session row")
