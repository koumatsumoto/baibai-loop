"""Pure price and action values; no adjustment calculations or acquisition."""

from __future__ import annotations

from dataclasses import dataclass as stdlib_dataclass
from datetime import date
from math import isfinite

from pydantic import ConfigDict, field_validator
from pydantic.dataclasses import dataclass

from baibai_engine.collection.identifiers import normalize_ticker

MODEL_CONFIG = ConfigDict(strict=True, arbitrary_types_allowed=False)


@stdlib_dataclass(frozen=True, slots=True)
class PriceBar:
    traded_at: date
    price: float
    turnover_value: float | None


def validate_finite(value: float | None) -> float | None:
    if value is not None and not isfinite(value):
        raise ValueError("numeric values must be finite")
    return value


@dataclass(frozen=True, slots=True, config=MODEL_CONFIG)
class JQuantsDailyBar:
    ticker: str
    traded_at: date
    close: float
    turnover_value: float | None
    # Traded shares. Separate from `turnover_value` because a balance expressed in
    # shares (margin interest) can only be turned into days of trading by a share
    # count; dividing yen turnover by the close would substitute the close for the
    # day's average price.
    volume: float | None = None
    adjustment_factor: float | None = None
    ex_right_type: str | None = None

    @field_validator("ticker", mode="before")
    @classmethod
    def _normalize_ticker_field(cls, value: str) -> str:
        return normalize_ticker(value)

    @field_validator("close", "turnover_value", "volume", "adjustment_factor")
    @classmethod
    def _finite_numeric_fields(cls, value: float | None) -> float | None:
        return validate_finite(value)


@dataclass(frozen=True, slots=True, config=MODEL_CONFIG)
class JQuantsAdjustmentFactorEvent:
    """Corporate-action factor whose existence does not depend on a traded close.

    An ex-rights row can legitimately have no close while trading is suspended.  Price
    bars require a close, but adjustment calculations require the independent event.
    Keeping the two records separate prevents a missing price from deleting the action.
    """

    ticker: str
    traded_at: date
    adjustment_factor: float
    ex_right_type: str | None = None

    @field_validator("ticker", mode="before")
    @classmethod
    def _normalize_ticker_field(cls, value: str) -> str:
        return normalize_ticker(value)

    @field_validator("adjustment_factor")
    @classmethod
    def _finite_adjustment_factor(cls, value: float) -> float:
        validated = validate_finite(value)
        assert validated is not None
        if validated <= 0:
            raise ValueError("adjustment_factor must be positive")
        return validated
