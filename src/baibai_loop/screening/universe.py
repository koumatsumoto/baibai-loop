from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date
from statistics import mean

from .providers.jquants import JQuantsDailyBar
from .schema import SecurityMaster, UniverseSnapshot

ELIGIBLE_MARKETS = {"PRIME", "STANDARD", "GROWTH", "プライム", "スタンダード", "グロース"}
DEFAULT_LISTED_UNDER_DAYS = 182
DEFAULT_REQUIRED_JPX_FLAGS = {"特別注意銘柄", "整理銘柄", "取引停止", "上場廃止警告"}


@dataclass(frozen=True)
class UniverseBuildResult:
    snapshots: Mapping[str, UniverseSnapshot]
    exclusion_counts: Mapping[str, int] = field(default_factory=dict)


def build_universe(
    asof_date: date,
    securities: Sequence[SecurityMaster],
    bars_by_ticker: Mapping[str, Sequence[JQuantsDailyBar]],
    shares_outstanding_by_ticker: Mapping[str, float | None],
    jpx_flags_by_ticker: Mapping[str, Sequence[str]],
    *,
    min_market_cap_oku: int = 100,
    min_avg_turnover_oku: float = 1.0,
    listed_under_days: int = DEFAULT_LISTED_UNDER_DAYS,
    required_jpx_flags: frozenset[str] = frozenset(DEFAULT_REQUIRED_JPX_FLAGS),
) -> UniverseBuildResult:
    snapshots: dict[str, UniverseSnapshot] = {}
    exclusion_counts: dict[str, int] = {}

    for security in securities:
        flags: list[str] = []
        market_name = security.market_segment.upper()
        if not security.is_common_stock:
            flags.append("non_common_stock")
        if market_name not in ELIGIBLE_MARKETS:
            flags.append("market_out_of_scope")

        history = sorted(bars_by_ticker.get(security.code, ()), key=lambda item: item.traded_at)
        # J-Quants v2 master does not expose listing_date; use the earliest available
        # daily bar as a listing-span proxy. Bars cache covers asof-1200d, so established
        # names show ~1200d and recent IPOs show days-since-listing accurately.
        listing_span_days = (asof_date - history[0].traded_at).days if history else 0
        if listing_span_days < listed_under_days:
            flags.append("listed_under_6_months")

        latest = history[-1] if history else None
        trailing_20 = history[-20:]
        if len(trailing_20) < 20 or latest is None:
            flags.append("insufficient_bar_history")
            avg_turnover_oku = None
            market_cap_oku = None
        else:
            turnovers = [
                bar.turnover_value for bar in trailing_20 if bar.turnover_value is not None
            ]
            avg_turnover_oku = mean(turnovers) / 100_000_000 if len(turnovers) == 20 else None
            shares = shares_outstanding_by_ticker.get(security.code)
            market_cap_oku = (latest.close * shares / 100_000_000) if shares else None
            if avg_turnover_oku is None:
                flags.append("missing_turnover_value")
            elif avg_turnover_oku < min_avg_turnover_oku:
                flags.append("avg_turnover_below_threshold")
            if market_cap_oku is None:
                flags.append("missing_market_cap")
            elif market_cap_oku < min_market_cap_oku:
                flags.append("market_cap_below_threshold")

        jpx_flags = tuple(sorted(set(jpx_flags_by_ticker.get(security.code, ()))))
        if any(flag in required_jpx_flags for flag in jpx_flags):
            flags.append("jpx_regulation")
            flags.extend(f"jpx:{flag}" for flag in jpx_flags)

        if flags:
            for flag in set(flags):
                exclusion_counts[flag] = exclusion_counts.get(flag, 0) + 1
            continue

        snapshots[security.code] = UniverseSnapshot(
            market_cap_oku=round(market_cap_oku) if market_cap_oku is not None else None,
            avg_turnover_oku=round(avg_turnover_oku, 1) if avg_turnover_oku is not None else None,
            exclusion_flags=(),
        )

    return UniverseBuildResult(
        snapshots=snapshots, exclusion_counts=dict(sorted(exclusion_counts.items()))
    )
