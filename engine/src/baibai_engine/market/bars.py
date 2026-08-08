from __future__ import annotations

from collections.abc import Sequence
from datetime import date
from math import isfinite

from pydantic import ConfigDict, field_validator
from pydantic.dataclasses import dataclass

from baibai_engine.market.ticker import normalize_ticker

MODEL_CONFIG = ConfigDict(strict=True, arbitrary_types_allowed=False)


def validate_finite(value: float | None) -> float | None:
    if value is not None and not isfinite(value):
        raise ValueError("numeric values must be finite")
    return value


def asof_basis_closes(bars: Sequence[JQuantsDailyBar]) -> list[float]:
    """昇順に並んだ bars の close を、最終行 (asof) の株式基準へ換算した系列を返す。

    incremental cache では adjustment_close の遡及調整が取得時期に依存して混在し、
    series としては使えない (分割後に取得した行だけ調整済みになり、境界で偽の
    ±50% 段差が生じる)。分割・併合イベントそのものである adjustment_factor は
    不変なので、close x (その行より後の factor 累積) で調整済み系列を自前で組む。
    factor は権利落ち日の bar に載り、その日より前の価格に適用される。
    """
    result = [0.0] * len(bars)
    factor = 1.0
    for index in range(len(bars) - 1, -1, -1):
        bar = bars[index]
        result[index] = bar.close * factor
        adjustment = bar.adjustment_factor
        if adjustment not in (None, 0.0, 1.0):
            assert adjustment is not None
            factor *= adjustment
    return result


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
    adjustment_close: float | None = None
    adjustment_factor: float | None = None

    @field_validator("ticker", mode="before")
    @classmethod
    def _normalize_ticker_field(cls, value: str) -> str:
        return normalize_ticker(value)

    @field_validator("close", "turnover_value", "volume", "adjustment_close", "adjustment_factor")
    @classmethod
    def _finite_numeric_fields(cls, value: float | None) -> float | None:
        return validate_finite(value)


@dataclass(frozen=True, slots=True, config=MODEL_CONFIG)
class JQuantsMarketCalendarDay:
    day: date
    is_business_day: bool
