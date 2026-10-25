"""Screening scope: every common stock with a usable price observation.

The data platform keeps facts for the whole market, so the screen evaluates
all common stocks in the eligible market segments. Size and liquidity are not
scope conditions: market cap, average turnover, price-history span, and JPX
regulation flags are recorded as facts on each snapshot and applied as
analysis-layer context or parameters by Candidate Discovery. The only structural exclusions are
instrument type, market segment, and a usable price observation. Each metric
handles its own history requirement.

Instrument eligibility uses the native product category and issue suffix
projected by the master reader, plus the eligible market segment. Sector names
group analysis; they do not stand in for the product classification.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date
from math import isfinite
from statistics import mean

from baibai_engine.machine_analysis.screening.schema import UniverseSnapshot
from baibai_engine.market.models import SecurityMaster
from baibai_engine.market.price_basis import asof_basis_closes
from baibai_engine.market.price_models import JQuantsAdjustmentFactorEvent, JQuantsDailyBar
from baibai_engine.market.universe import ELIGIBLE_MARKETS as ELIGIBLE_MARKETS

from .rule_config import ScreeningRules

# 財務評価には有効な価格1本が必要。平均売買代金は別の20本窓 (#1518)。
# docs/reference/screening-runtime.md#input-scope
MIN_BAR_HISTORY = 1
AVG_TURNOVER_WINDOW_BARS = 20
# Scope changes must give saved runs, candidate contexts and calibration a new identity.
UNIVERSE_SCOPE_REVISION = "native-common-stock-one-bar-v3"


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
    snapshots: dict[str, UniverseSnapshot] = {}
    exclusion_counts: dict[str, int] = {}
    exclusion_flags: dict[str, tuple[str, ...]] = {}

    for security in securities:
        flags: list[str] = []
        market_name = security.market_segment.upper()
        if not security.is_common_stock:
            flags.append("non_common_stock")
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

        # Measure available price-history coverage, not listing age. A bounded or incomplete
        # price history can shorten the measured span even for a long-listed security.
        price_history_span_days = (asof_date - history[0].traded_at).days
        latest = history[-1]
        trailing_20 = history[-AVG_TURNOVER_WINDOW_BARS:]
        turnovers = [bar.turnover_value for bar in trailing_20 if bar.turnover_value is not None]
        avg_turnover_oku = (
            mean(turnovers) / 100_000_000 if len(turnovers) == AVG_TURNOVER_WINDOW_BARS else None
        )
        shares = shares_outstanding_by_ticker.get(security.ticker)
        adjustment_events = (
            adjustment_events_by_ticker.get(security.ticker, ())
            if adjustment_events_by_ticker is not None
            else history
        )
        # Only events after the latest close change its as-of price basis.
        try:
            latest_price = (
                asof_basis_closes([latest], adjustment_events, asof_date=asof_date)[0]
                if latest.close > 0
                else None
            )
        except ValueError:
            latest_price = None
        if latest_price is None or not isfinite(latest_price) or latest_price <= 0:
            exclusion_flags[security.ticker] = ("invalid_price_basis",)
            exclusion_counts["invalid_price_basis"] = (
                exclusion_counts.get("invalid_price_basis", 0) + 1
            )
            continue
        market_cap_oku = (latest_price * shares / 100_000_000) if shares else None

        snapshots[security.ticker] = UniverseSnapshot(
            market_cap_oku=round(market_cap_oku) if market_cap_oku is not None else None,
            avg_turnover_oku=round(avg_turnover_oku, 1) if avg_turnover_oku is not None else None,
            price_history_span_days=price_history_span_days,
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
            price_history_span_days=snapshot.price_history_span_days,
            jpx_flags=snapshot.jpx_flags,
            required_jpx_flags=required_jpx,
            require_facts=True,
        )
    )
