"""全普通株の薄い同時点観測。経路や表示枠を投資判断にしない。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from typing import Annotated, Final, Literal, TypedDict

from pydantic import BaseModel, ConfigDict, Field, JsonValue, StrictInt, TypeAdapter, with_config

DISCOVERY_METHOD: Final = "opportunity-discovery-v1"


class DiscoveryPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    method: Literal["opportunity-discovery-v1"] = DISCOVERY_METHOD


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
class DiscoveryRow(TypedDict):
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
    price: ObservedNumber | None
    price_date: str | None
    expected_price_date: str | None
    basis: str | None
    price_ready: bool


@dataclass(frozen=True, slots=True)
class DiscoveryIndexRead:
    status: Literal["no_publication", "unindexed_legacy", "indexed"]
    selection_id: str | None = None
    generation_id: str | None = None
    as_of: date | None = None
    evaluation_time: datetime | None = None
    published_at: datetime | None = None
    rows: tuple[DiscoveryRow, ...] = ()
    universe_tickers: tuple[str, ...] = ()

    def listing_dates(self) -> dict[str, tuple[str | None, str | None, str | None]]:
        """参照日照合用。財務モデルの株価を差し替えない。"""
        return {
            r["ticker"]: (r["sector"], r["price_date"], r["expected_price_date"]) for r in self.rows
        }


@dataclass(frozen=True, slots=True)
class DiscoverySelection:
    policy: DiscoveryPolicy
    rows: tuple[DiscoveryRow, ...]
    summary: dict[str, JsonValue]

    @property
    def tickers(self) -> tuple[str, ...]:
        """旧レーン外・未観測も探索母集団。順位・買い適格集合ではない。"""
        return tuple(row["ticker"] for row in self.rows)


def discovery_publication_id(generation_id: str) -> str:
    return f"selection-{generation_id}-{DISCOVERY_METHOD}"


def is_discovery_publication(selection_id: str, generation_id: str) -> bool:
    if selection_id == discovery_publication_id(generation_id):
        return True
    suffix = selection_id.removeprefix(f"selection-{generation_id}-")
    if suffix.startswith("opportunity-discovery-") or "-opportunity-discovery-" in suffix:
        raise ValueError("unknown discovery publication version or identity")
    return False


_ROW_CODEC = TypeAdapter(tuple[DiscoveryRow, ...])


def require_publishable_rows(result: DiscoverySelection) -> None:
    # TypedDict values can change between projection and publication.
    _ROW_CODEC.validate_python(result.rows, strict=True)
