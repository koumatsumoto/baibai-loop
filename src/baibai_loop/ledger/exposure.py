from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass

from .trades import TradeRecord

# 2026-05 retro の運用ルール「同一 sector / playbook の deployed notional が
# 50% を超えたら、追加 entry 前に exposure review を必須にする」の事前固定閾値。
EXPOSURE_WARNING_SHARE = 0.5

_UNKNOWN_KEY = "unknown"


@dataclass(frozen=True, slots=True)
class ExposureBucket:
    key: str
    notional: float
    share: float
    tickers: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ExposureReport:
    total_notional: float
    by_sector: tuple[ExposureBucket, ...]
    by_playbook: tuple[ExposureBucket, ...]
    by_ticker: tuple[ExposureBucket, ...]
    warnings: tuple[str, ...]


def compute_exposure(
    trades: Sequence[TradeRecord],
    sector_by_ticker: Mapping[str, str],
) -> ExposureReport:
    """Aggregate open-position entry notional into sector / playbook / ticker shares.

    Entry notional is the same basis the monthly retro uses to attribute
    concentration, so the warning threshold reads identically in both places.
    A ticker missing from the master snapshot falls into the ``unknown`` bucket
    instead of being dropped: concentration must not shrink because a fact is
    missing.
    """
    total_notional = sum(trade.entry_price * trade.quantity for trade in trades)
    by_sector = _buckets(
        trades,
        total_notional,
        lambda trade: sector_by_ticker.get(trade.ticker) or _UNKNOWN_KEY,
    )
    by_playbook = _buckets(
        trades,
        total_notional,
        lambda trade: trade.playbook_id or _UNKNOWN_KEY,
    )
    by_ticker = _buckets(trades, total_notional, lambda trade: trade.ticker)
    warnings = [
        *_threshold_warnings("sector", by_sector),
        *_threshold_warnings("playbook", by_playbook),
    ]
    return ExposureReport(
        total_notional=total_notional,
        by_sector=by_sector,
        by_playbook=by_playbook,
        by_ticker=by_ticker,
        warnings=tuple(warnings),
    )


def _buckets(
    trades: Sequence[TradeRecord],
    total_notional: float,
    key_of: Callable[[TradeRecord], str],
) -> tuple[ExposureBucket, ...]:
    notional_by_key: dict[str, float] = defaultdict(float)
    tickers_by_key: dict[str, set[str]] = defaultdict(set)
    for trade in trades:
        key = key_of(trade)
        notional_by_key[key] += trade.entry_price * trade.quantity
        tickers_by_key[key].add(trade.ticker)
    buckets = [
        ExposureBucket(
            key=key,
            notional=notional,
            share=notional / total_notional if total_notional > 0 else 0.0,
            tickers=tuple(sorted(tickers_by_key[key])),
        )
        for key, notional in notional_by_key.items()
    ]
    buckets.sort(key=lambda bucket: (-bucket.notional, bucket.key))
    return tuple(buckets)


def _threshold_warnings(label: str, buckets: Sequence[ExposureBucket]) -> list[str]:
    return [
        f"{label} {bucket.key} holds {bucket.share * 100:.1f}% of deployed notional "
        f"(>= {EXPOSURE_WARNING_SHARE * 100:.0f}%); exposure review required before adding"
        for bucket in buckets
        if bucket.share > EXPOSURE_WARNING_SHARE
    ]
