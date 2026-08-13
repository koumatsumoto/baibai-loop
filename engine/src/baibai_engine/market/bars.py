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


def asof_basis_closes(
    bars: Sequence[JQuantsDailyBar],
    adjustment_events: Sequence[JQuantsAdjustmentFactorEvent | JQuantsDailyBar] | None = None,
    *,
    asof_date: date | None = None,
) -> list[float]:
    """昇順に並んだ bars の close を、指定asofの株式基準へ換算した系列を返す。

    `asof_date`を省略した場合だけ最終barの日を基準にする。価格が無いaction日を明示asofに
    指定した場合も、その日までのfactorを適用する。

    incremental cache では adjustment_close の遡及調整が取得時期に依存して混在し、
    series としては使えない (分割後に取得した行だけ調整済みになり、境界で偽の
    ±50% 段差が生じる)。分割・併合イベントそのものである adjustment_factor は
    不変なので、close x (その行より後の factor 累積) で調整済み系列を自前で組む。
    factor は権利落ち日の bar に載り、その日より前の価格に適用される。
    """
    if not bars:
        return []
    events = sorted(
        (
            event
            for event in (bars if adjustment_events is None else adjustment_events)
            if event.adjustment_factor not in (None, 0.0, 1.0)
            and event.traded_at <= (asof_date or bars[-1].traded_at)
        ),
        key=lambda event: event.traded_at,
    )
    result = [0.0] * len(bars)
    factor = 1.0
    event_index = len(events) - 1
    for index in range(len(bars) - 1, -1, -1):
        bar = bars[index]
        while event_index >= 0 and events[event_index].traded_at > bar.traded_at:
            adjustment = events[event_index].adjustment_factor
            assert adjustment is not None
            factor *= adjustment
            event_index -= 1
        result[index] = bar.close * factor
        while event_index >= 0 and events[event_index].traded_at == bar.traded_at:
            adjustment = events[event_index].adjustment_factor
            assert adjustment is not None
            factor *= adjustment
            event_index -= 1
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
class JQuantsAdjustmentFactorEvent:
    """Corporate-action factor whose existence does not depend on a traded close.

    An ex-rights row can legitimately have no close while trading is suspended.  Price
    bars require a close, but split-basis normalization requires only this event tuple.
    Keeping the two records separate prevents a missing price from deleting the action.
    """

    ticker: str
    traded_at: date
    adjustment_factor: float

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


@dataclass(frozen=True, slots=True, config=MODEL_CONFIG)
class JQuantsMarketCalendarDay:
    day: date
    is_business_day: bool
