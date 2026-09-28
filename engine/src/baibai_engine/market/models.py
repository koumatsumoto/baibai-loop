"""Source security identity, independent of Screening outputs."""

from __future__ import annotations

from typing import Annotated

from pydantic import ConfigDict, Field, field_validator
from pydantic.dataclasses import dataclass

from baibai_engine.market.ticker import normalize_ticker

_MODEL_CONFIG = ConfigDict(strict=True, arbitrary_types_allowed=False, validate_assignment=False)
type Ticker = Annotated[str, Field(pattern=r"^[0-9A-Z]{4}$")]
type NonEmptyString = Annotated[str, Field(min_length=1)]


@dataclass(frozen=True, slots=True, config=_MODEL_CONFIG)
class SecurityMaster:
    ticker: Ticker
    name: NonEmptyString
    market_segment: NonEmptyString
    sector_33: NonEmptyString
    is_common_stock: bool

    @field_validator("ticker", mode="before")
    @classmethod
    def _normalize_ticker(cls, value: str) -> str:
        return normalize_ticker(value)
