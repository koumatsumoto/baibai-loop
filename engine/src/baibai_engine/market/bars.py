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
    """昇順に並んだ bars の close を、指定asofの価格調整基準へ換算した系列を返す。

    `asof_date`を省略した場合だけ最終barの日を基準にする。価格が無いaction日を明示asofに
    指定した場合も、その日までのfactorを適用する。

    配信元の調整済み価格は遡及調整が取得時期に依存して混在し、
    series としては使えない (分割後に取得した行だけ調整済みになり、境界で偽の
    ±50% 段差が生じる)。close x (その行より後の価格調整factor累積) で系列を組む。
    factor は権利落ち日の bar に載り、その日より前の価格に適用される。
    配信元による係数の訂正はあり得る。ライツイシューの価格調整は株数の単位変更ではない。
    """
    if not bars:
        return []
    return asof_basis_prices(
        [(bar.traded_at, bar.close) for bar in bars],
        [
            (event.traded_at, event.adjustment_factor)
            for event in (bars if adjustment_events is None else adjustment_events)
        ],
        asof_date=asof_date or bars[-1].traded_at,
    )


def asof_basis_prices(
    prices: Sequence[tuple[date, float]],
    adjustment_events: Sequence[tuple[date, float | None]],
    *,
    asof_date: date,
) -> list[float]:
    """観測価格と独立factorからas-of基準の価格を組む、共有価格算術。

    callerはasof以下の価格を日付昇順で渡す。出力はその価格列と同じ順序になる。
    factor側の日付制限と整列はこの関数が行う。
    価格換算に使う係数は有限の正数でなければならない。0を調整なしとは扱わない。
    """
    if not prices:
        return []
    events = sorted(
        (
            event
            for event in adjustment_events
            if event[1] not in (None, 1.0) and prices[0][0] < event[0] <= asof_date
        ),
        key=lambda event: event[0],
    )
    result = [0.0] * len(prices)
    factor = 1.0
    event_index = len(events) - 1
    for index in range(len(prices) - 1, -1, -1):
        traded_at, close = prices[index]
        while event_index >= 0 and events[event_index][0] > traded_at:
            adjustment = events[event_index][1]
            assert adjustment is not None
            if not isfinite(adjustment) or adjustment <= 0:
                raise ValueError("price adjustment factor must be finite and positive")
            factor *= adjustment
            event_index -= 1
        result[index] = close * factor
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


def share_unit_factor(event: JQuantsAdjustmentFactorEvent | JQuantsDailyBar) -> float | None:
    """Return a share-unit conversion, not the provider's price adjustment.

    ExRT 1/2 identifies splits/reverse splits; 3 is a rights issue and does not
    rescale an existing share. A nontrivial price factor without a recognized kind
    cannot establish a share-unit conversion. Missing ordinary observations do not
    invent an action; completeness remains the source reader's responsibility.
    """
    factor = event.adjustment_factor
    if event.ex_right_type == "3":
        return 1.0
    if event.ex_right_type in ("1", "2"):
        return factor if factor is not None and factor > 0 else None
    if event.ex_right_type is None and factor in (None, 1.0):
        return 1.0
    return None


@dataclass(frozen=True, slots=True, config=MODEL_CONFIG)
class JQuantsMarketCalendarDay:
    day: date
    is_business_day: bool
