"""全普通株の薄い同時点観測。経路や表示枠を投資判断にしない。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, TypedDict

from pydantic import BaseModel, ConfigDict, JsonValue


class DiscoveryPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    method: Literal["opportunity-discovery-v1"] = "opportunity-discovery-v1"


class DiscoveryRow(TypedDict):
    ticker: str
    models_recorded: bool
    name: str | None
    sector: str | None
    screening_lane: str
    technical_state: str
    financial_state: str
    matched_paths: dict[str, list[str]]
    missing: list[str]
    coordinates: list[dict[str, JsonValue]]
    price: float | int | None
    price_date: str | None
    expected_price_date: str | None
    basis: str | None
    price_ready: bool
    book_equity: dict[str, JsonValue] | None
    capital_action: dict[str, int | None] | None


@dataclass(frozen=True, slots=True)
class DiscoverySelection:
    policy: DiscoveryPolicy
    rows: tuple[DiscoveryRow, ...]
    summary: dict[str, JsonValue]

    @property
    def tickers(self) -> tuple[str, ...]:
        """旧レーン外・未観測も探索母集団。順位・買い適格集合ではない。"""
        return tuple(row["ticker"] for row in self.rows)
