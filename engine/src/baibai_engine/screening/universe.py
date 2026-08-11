"""Screening scope: every common stock with enough bar history to evaluate.

The data platform keeps facts for the whole market, so the screen evaluates
all common stocks in the eligible market segments. Size and liquidity are not
scope conditions: market cap, average turnover, listing span, and JPX
regulation flags are recorded as facts on each snapshot and applied as
analysis-layer parameters by selection. The only structural exclusions are
instrument type, market segment, and a minimal bar-history requirement that the
metric pipeline needs to compute short-horizon fields.

Instrument type is decided by the sector classification rather than by the
master's own flag. The exchange assigns a 33-sector code to common stock only,
and it is the one identifier the source actually fills in; the master's
`is_common_stock` reads true for every row it has ever held, so a screen that
relied on it would be relying on a constant.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date
from statistics import mean

from .providers.jquants import JQuantsDailyBar
from .rule_config import ScreeningRules
from .schema import SecurityMaster, UniverseSnapshot

# The scope is the main domestic markets: everything a private investor can buy on
# ordinary terms, less TOKYO PRO MARKET and the residual segments. The exchange
# renamed its segments in April 2022, so a point-in-time master from before then
# carries the older names for the same markets. Both vocabularies are listed
# because the scope is about which markets, not about what they are called; a
# current master never carries the older names, so historical replay is the only
# place they appear.
ELIGIBLE_MARKETS = {
    "PRIME",
    "STANDARD",
    "GROWTH",
    "プライム",
    "スタンダード",
    "グロース",
    "東証一部",
    "東証二部",
    "マザーズ",
    "JASDAQ スタンダード",
    "JASDAQ グロース",
}

# 東証 33 業種。分類は普通株にのみ付与されるので、これに載らない銘柄は普通株ではない。
# `is_common_stock` は判別材料にならない: master の証券種別 field を J-Quants が返さない
# ため取得側の判定が常に真へ落ち、store の 568,329 行すべてで 1 になっている。業種分類は
# source が実際に答えている唯一の識別子なので、instrument type の除外はここから作る。
TSE_33_SECTORS = frozenset(
    {
        "水産・農林業",
        "鉱業",
        "建設業",
        "食料品",
        "繊維製品",
        "パルプ・紙",
        "化学",
        "医薬品",
        "石油・石炭製品",
        "ゴム製品",
        "ガラス・土石製品",
        "鉄鋼",
        "非鉄金属",
        "金属製品",
        "機械",
        "電気機器",
        "輸送用機器",
        "精密機器",
        "その他製品",
        "電気・ガス業",
        "陸運業",
        "海運業",
        "空運業",
        "倉庫・運輸関連業",
        "情報・通信業",
        "卸売業",
        "小売業",
        "銀行業",
        "証券・商品先物取引業",
        "保険業",
        "その他金融業",
        "不動産業",
        "サービス業",
    }
)
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
        if security.sector_33 not in TSE_33_SECTORS:
            flags.append("sector_out_of_classification")
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
