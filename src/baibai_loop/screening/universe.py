"""Screening scope: every common stock with enough bar history to evaluate.

The data platform keeps facts for the whole market, so the screen evaluates
all common stocks in the eligible market segments. Size and liquidity are not
scope conditions: market cap, average turnover, listing span, and JPX
regulation flags are recorded as facts on each snapshot and applied as
analysis-layer parameters by selection. The only structural exclusions are
instrument type (non-common stock), market segment, and a minimal bar-history
requirement that the metric pipeline needs to compute short-horizon fields.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date
from statistics import mean

from .providers.jquants import JQuantsDailyBar
from .rule_config import ScreeningRules
from .schema import SecurityMaster, UniverseSnapshot

ELIGIBLE_MARKETS = {"PRIME", "STANDARD", "GROWTH", "プライム", "スタンダード", "グロース"}
MIN_BAR_HISTORY = 20


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
        if len(history) < MIN_BAR_HISTORY:
            flags.append("insufficient_bar_history")

        if flags:
            for flag in set(flags):
                exclusion_counts[flag] = exclusion_counts.get(flag, 0) + 1
            continue

        # J-Quants v2 master does not expose listing_date; use the earliest available
        # daily bar as a listing-span proxy. Bars cache covers asof-1200d, so established
        # names show ~1200d and recent IPOs show days-since-listing accurately.
        listing_span_days = (asof_date - history[0].traded_at).days
        latest = history[-1]
        trailing_20 = history[-MIN_BAR_HISTORY:]
        turnovers = [bar.turnover_value for bar in trailing_20 if bar.turnover_value is not None]
        avg_turnover_oku = (
            mean(turnovers) / 100_000_000 if len(turnovers) == MIN_BAR_HISTORY else None
        )
        shares = shares_outstanding_by_ticker.get(security.code)
        market_cap_oku = (latest.close * shares / 100_000_000) if shares else None

        snapshots[security.code] = UniverseSnapshot(
            market_cap_oku=round(market_cap_oku) if market_cap_oku is not None else None,
            avg_turnover_oku=round(avg_turnover_oku, 1) if avg_turnover_oku is not None else None,
            listing_span_days=listing_span_days,
            jpx_flags=tuple(sorted(set(jpx_flags_by_ticker.get(security.code, ())))),
        )

    return UniverseBuildResult(
        snapshots=snapshots, exclusion_counts=dict(sorted(exclusion_counts.items()))
    )


def liquid_median_population(
    snapshots: Mapping[str, UniverseSnapshot],
    rules: ScreeningRules,
) -> frozenset[str]:
    """Tickers whose facts satisfy the selection liquidity parameters.

    Sector / market medians and sector relative strength compare against this
    investable population so the screen's relative-valuation judgments stay
    anchored to liquid comparables while every common stock is evaluated. Uses
    the base-config liquidity rules directly; programmatic in-process overrides
    apply only to the selection filter, not to this population.
    """
    liquidity = rules.selection.liquidity
    required_jpx = frozenset(rules.universe.required_jpx_flags)
    return frozenset(
        ticker
        for ticker, snapshot in snapshots.items()
        if liquidity.matches(
            market_cap_oku=snapshot.market_cap_oku,
            avg_turnover_oku=snapshot.avg_turnover_oku,
            listing_span_days=snapshot.listing_span_days,
            jpx_flags=snapshot.jpx_flags,
            required_jpx_flags=required_jpx,
            require_facts=True,
        )
    )
