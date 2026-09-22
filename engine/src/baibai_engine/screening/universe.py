"""Screening scope: every common stock with enough bar history to evaluate.

The data platform keeps facts for the whole market, so the screen evaluates
all common stocks in the eligible market segments. Size and liquidity are not
scope conditions: market cap, average turnover, listing span, and JPX
regulation flags are recorded as facts on each snapshot and applied as
analysis-layer context or parameters by Candidate Discovery. The only structural exclusions are
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

from baibai_engine.market.bars import JQuantsAdjustmentFactorEvent, asof_basis_closes
from baibai_engine.market.universe import (
    ELIGIBLE_MARKETS as ELIGIBLE_MARKETS,
)
from baibai_engine.market.universe import (
    NON_COMMON_STOCK_SECTORS as NON_COMMON_STOCK_SECTORS,
)
from baibai_engine.market.universe import (
    TSE_33_SECTORS as TSE_33_SECTORS,
)
from baibai_engine.market.universe import (
    UniverseSourceDriftError as UniverseSourceDriftError,
)

from .providers.jquants import JQuantsDailyBar
from .rule_config import ScreeningRules
from .schema import SecurityMaster, UniverseSnapshot

MIN_BAR_HISTORY = 20


@dataclass(frozen=True)
class UniverseBuildResult:
    snapshots: Mapping[str, UniverseSnapshot]
    exclusion_counts: Mapping[str, int] = field(default_factory=dict)
    # 除外された銘柄ごとの理由。読み手が同じ判定を書き直さずに済むよう、判定した側が
    # 結果を持つ。書き直すと、条件を 1 つ足した日に 2 つの surface が別の量になる。
    exclusion_flags: Mapping[str, tuple[str, ...]] = field(default_factory=dict)


# 方針で外す理由。data が足りずに外れる理由 (`insufficient_bar_history`) と対を成し、
# 「意図して除いた」と「評価できなかった」の境界はこの集合が定める。
POLICY_EXCLUSION_REASONS: tuple[str, ...] = (
    "non_common_stock",
    "sector_out_of_classification",
    "market_out_of_scope",
)


def build_universe(
    asof_date: date,
    securities: Sequence[SecurityMaster],
    bars_by_ticker: Mapping[str, Sequence[JQuantsDailyBar]],
    shares_outstanding_by_ticker: Mapping[str, float | None],
    jpx_flags_by_ticker: Mapping[str, Sequence[str]],
    adjustment_events_by_ticker: Mapping[
        str, Sequence[JQuantsAdjustmentFactorEvent | JQuantsDailyBar]
    ]
    | None = None,
) -> UniverseBuildResult:
    # 母集団の定義を業種名の完全一致に預けているので、source の語彙が動いた日は
    # 「その業種の全銘柄が普通株でない」と読める。件数は減るだけで例外は出ないため、
    # 知らない語を見た時点で止める。個別の除外より先に、語彙そのものを検査する。
    #
    # 検査は適格市場区分の行だけに掛ける。区分で既に外れる行の語彙が変わっても母集団は
    # 1 行も動かないので、そこで止めると守っていない対象のために可用性を失う。
    unknown_sectors = sorted(
        {
            security.sector_33
            for security in securities
            if security.market_segment.upper() in ELIGIBLE_MARKETS
            and security.sector_33 not in TSE_33_SECTORS
            and security.sector_33 not in NON_COMMON_STOCK_SECTORS
        }
    )
    if unknown_sectors:
        raise UniverseSourceDriftError(
            f"unknown sector classification on an eligible market: {unknown_sectors}; "
            "the universe excludes on exact sector names, so an unrecognised label would "
            "silently drop every stock carrying it. Add it to TSE_33_SECTORS when it names "
            "a common-stock sector, or to NON_COMMON_STOCK_SECTORS when it names an "
            "instrument type that is not common stock."
        )

    snapshots: dict[str, UniverseSnapshot] = {}
    exclusion_counts: dict[str, int] = {}
    exclusion_flags: dict[str, tuple[str, ...]] = {}

    for security in securities:
        flags: list[str] = []
        market_name = security.market_segment.upper()
        if not security.is_common_stock:
            flags.append("non_common_stock")
        if security.sector_33 not in TSE_33_SECTORS:
            flags.append("sector_out_of_classification")
        if market_name not in ELIGIBLE_MARKETS:
            flags.append("market_out_of_scope")

        history = sorted(
            (bar for bar in bars_by_ticker.get(security.ticker, ()) if bar.traded_at <= asof_date),
            key=lambda item: item.traded_at,
        )
        if len(history) < MIN_BAR_HISTORY:
            flags.append("insufficient_bar_history")

        if flags:
            exclusion_flags[security.ticker] = tuple(flags)
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
        shares = shares_outstanding_by_ticker.get(security.ticker)
        adjustment_events = (
            adjustment_events_by_ticker.get(security.ticker, ())
            if adjustment_events_by_ticker is not None
            else history
        )
        latest_price = asof_basis_closes([latest], adjustment_events, asof_date=asof_date)[0]
        market_cap_oku = (latest_price * shares / 100_000_000) if shares else None

        snapshots[security.ticker] = UniverseSnapshot(
            market_cap_oku=round(market_cap_oku) if market_cap_oku is not None else None,
            avg_turnover_oku=round(avg_turnover_oku, 1) if avg_turnover_oku is not None else None,
            listing_span_days=listing_span_days,
            jpx_flags=tuple(sorted(set(jpx_flags_by_ticker.get(security.ticker, ())))),
        )

    return UniverseBuildResult(
        snapshots=snapshots,
        exclusion_counts=dict(sorted(exclusion_counts.items())),
        exclusion_flags=exclusion_flags,
    )


def candidate_comparison_population(
    snapshots: Mapping[str, UniverseSnapshot],
    rules: ScreeningRules,
) -> frozenset[str]:
    """Tickers whose facts satisfy Candidate Discovery common eligibility.

    Sector / market medians and sector relative strength compare against this
    population while every common stock is evaluated. Average turnover remains
    context and does not affect membership. Production and calibration callers
    pass the same rules instance so their populations remain identical.
    """
    eligibility = rules.candidate_discovery.common_eligibility
    required_jpx = frozenset(rules.universe.required_jpx_flags)
    return frozenset(
        ticker
        for ticker, snapshot in snapshots.items()
        if eligibility.matches(
            market_cap_oku=snapshot.market_cap_oku,
            listing_span_days=snapshot.listing_span_days,
            jpx_flags=snapshot.jpx_flags,
            required_jpx_flags=required_jpx,
            require_facts=True,
        )
    )
