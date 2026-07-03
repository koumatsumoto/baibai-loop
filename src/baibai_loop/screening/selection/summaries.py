"""Candidate tag and summary rendering for selection payload output."""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping, Sequence

from baibai_loop.foundation.coerce import (
    dedupe_strings,
    mapping_or_empty,
    string_or_none,
    string_sequence,
)

from .lenses import _durability_lens_of

# Nikkei に 3pt 以上劣後している候補を事前固定の annotation 閾値で注記する。
# 割安 (相対劣後) を買うのが本流のため ranking / gate には使わず、entry
# preflight の情報系列として research 側で参照する。
BENCHMARK_LAGGARD_RELATIVE_20D_MAX = -0.03

# 上場 750 暦日以上なのに直近 750 暦日の bar 本数が population 最大の 80% を
# 下回る銘柄は、自己レンジ / sigma gap が前提にする 3 年履歴に長期ギャップが
# ある(新規上場は short_history_flag 側で扱う)。事前固定の annotation 閾値。
PRICE_HISTORY_GAP_COVERAGE_MIN = 0.8
_PRICE_HISTORY_GAP_MIN_LISTING_SPAN_DAYS = 750


def _durability_counts(candidates: Sequence[Mapping[str, object]]) -> dict[str, int]:
    counts: Counter[str] = Counter()
    for candidate in candidates:
        rating = string_or_none(_durability_lens_of(candidate).get("rating")) or "unknown"
        counts[rating] += 1
    return dict(counts)


def _candidate_reason_tags(candidate: Mapping[str, object]) -> list[str]:
    tags: list[str] = []
    playbook = string_or_none(candidate.get("selection_playbook"))
    if playbook:
        tags.append(playbook)
    durability = _durability_lens_of(candidate)
    rating = string_or_none(durability.get("rating"))
    if rating == "high":
        tags.append("durability_high")
    elif rating == "medium":
        tags.append("durability_medium")
    return dedupe_strings(tags)


def _candidate_risk_tags(candidate: Mapping[str, object]) -> list[str]:
    tags: list[str] = []
    if candidate.get("previous_candidate") is True:
        tags.append("previous_candidate")
    benchmark_relative_20d = candidate.get("benchmark_relative_20d")
    if (
        isinstance(benchmark_relative_20d, int | float)
        and benchmark_relative_20d <= BENCHMARK_LAGGARD_RELATIVE_20D_MAX
    ):
        tags.append("benchmark_laggard_20d")
    coverage = candidate.get("price_history_coverage_750d")
    listing_span_days = candidate.get("listing_span_days")
    if (
        isinstance(coverage, int | float)
        and coverage < PRICE_HISTORY_GAP_COVERAGE_MIN
        and isinstance(listing_span_days, int | float)
        and listing_span_days >= _PRICE_HISTORY_GAP_MIN_LISTING_SPAN_DAYS
    ):
        tags.append("price_history_gap")
    # 分割・併合の直後は master 株数と価格の基準日ずれで market_cap / net_cash 比率 /
    # 価格変化率の fact が歪み得る (corporate action 未反映)。research 側で AP-03 の
    # corporate action check を必ず通すよう triage 段階で注意を立てる。
    if candidate.get("split_adjustment_flag") is True:
        tags.append("split_adjustment_recent")
    if candidate.get("suppressed") is True:
        tags.append("suppressed_by_prior_research")
    if string_or_none(candidate.get("next_earnings_date")):
        tags.append("earnings_scheduled")
    if candidate.get("freshness_warnings"):
        tags.append("freshness_warning")
    return dedupe_strings(tags)


def _selection_candidate_summary(
    candidate: Mapping[str, object], *, rank: int
) -> dict[str, object]:
    durability_lens = _durability_lens_of(candidate)
    metrics = mapping_or_empty(candidate.get("metrics"))
    return {
        "rank": rank,
        "ticker": string_or_none(candidate.get("ticker")),
        "name": string_or_none(candidate.get("name")),
        "sector_33": string_or_none(candidate.get("sector_33")),
        "selection_playbook": string_or_none(candidate.get("selection_playbook")),
        "macro_context_alignment": string_or_none(candidate.get("macro_context_alignment")),
        "market_cap_oku": candidate.get("market_cap_oku"),
        # liquidity: 5% ADV 参加上限で発注可能サイズを判断し、約定できない薄商いを弾く
        "avg_turnover_oku": candidate.get("avg_turnover_oku"),
        # valuation: triage 時に割安度を即判断できるよう転記する。ticker-profile を別途引かずに済む
        "per_trailing": candidate.get("per_trailing"),
        "per_forward": candidate.get("per_forward"),
        "pbr": candidate.get("pbr"),
        "ev_ebitda": candidate.get("ev_ebitda"),
        "p_s": candidate.get("p_s"),
        "pcfr": candidate.get("pcfr"),
        # downside protection: net-net / cash-rich の下値判断に使う財務指標
        "cash_to_market_cap": metrics.get("cash_to_market_cap"),
        "net_cash_to_market_cap": metrics.get("net_cash_to_market_cap"),
        "equity_ratio": metrics.get("equity_ratio"),
        "ocf_yield": metrics.get("ocf_yield"),
        # earnings momentum + cash conversion: 割安な trailing PER が減益・低 cash 変換を
        # 隠す value trap を triage で弾くための質シグナル
        "operating_profit_yoy": metrics.get("operating_profit_yoy"),
        "sales_yoy": metrics.get("sales_yoy"),
        "fcf_yield": metrics.get("fcf_yield"),
        # 機械 E[r] (成分分解付き見積り・%/年の比率) と FV アンカー。thesis の
        # FV 見積りの機械的出発点で、単一の合成スコアではない (estimates.py)。
        "er_annual": metrics.get("er_annual"),
        "er_reversion_annual": metrics.get("er_reversion_annual"),
        "er_carry_annual": metrics.get("er_carry_annual"),
        "er_anchor_metrics": metrics.get("er_anchor_metrics"),
        "fv_sector_median_yen": metrics.get("fv_sector_median_yen"),
        "fv_self_range_yen": metrics.get("fv_self_range_yen"),
        "dividend_yield": metrics.get("dividend_yield"),
        "price_change_5d": candidate.get("price_change_5d"),
        "price_change_20d": candidate.get("price_change_20d"),
        # dislocation 深度: 売られすぎ度の主要 window。割安ゾーン入りの経緯と RR の前提
        "price_change_60d": candidate.get("price_change_60d"),
        "benchmark_relative_20d": candidate.get("benchmark_relative_20d"),
        "gap_from_52w_low": candidate.get("gap_from_52w_low"),
        "price_history_coverage_750d": candidate.get("price_history_coverage_750d"),
        "split_adjustment_flag": candidate.get("split_adjustment_flag") is True,
        "next_earnings_date": candidate.get("next_earnings_date"),
        "position_tier": candidate.get("position_tier"),
        "durability_rating": string_or_none(durability_lens.get("rating")),
        "durability_caution_reasons": list(string_sequence(durability_lens.get("caution_reasons"))),
        "prior_research": candidate.get("prior_research"),
        "previous_candidate": candidate.get("previous_candidate") is True,
        "suppressed": candidate.get("suppressed") is True,
        "suppression_reasons": list(string_sequence(candidate.get("suppression_reasons"))),
        "reason_tags": list(string_sequence(candidate.get("reason_tags"))),
        "risk_tags": list(string_sequence(candidate.get("risk_tags"))),
    }


def _sweep_candidate_summary(candidate: Mapping[str, object], *, rank: int) -> dict[str, object]:
    durability_lens = _durability_lens_of(candidate)
    return {
        "rank": rank,
        "ticker": string_or_none(candidate.get("ticker")),
        "name": string_or_none(candidate.get("name")),
        "selection_playbook": string_or_none(candidate.get("selection_playbook")),
        "benchmark_relative_20d": candidate.get("benchmark_relative_20d"),
        "durability_rating": string_or_none(durability_lens.get("rating")),
        "previous_candidate": candidate.get("previous_candidate") is True,
        "reason_tags": list(string_sequence(candidate.get("reason_tags"))),
        "risk_tags": list(string_sequence(candidate.get("risk_tags"))),
    }


def _sweep_changed_summaries(
    base_by_ticker: Mapping[str, Mapping[str, object]],
    current_by_ticker: Mapping[str, Mapping[str, object]],
) -> list[dict[str, object]]:
    changed: list[dict[str, object]] = []
    for ticker in sorted(set(base_by_ticker) & set(current_by_ticker)):
        base = base_by_ticker[ticker]
        current = current_by_ticker[ticker]
        changed_fields = {
            key
            for key in (
                "durability_rating",
                "selection_playbook",
                "rank",
            )
            if base.get(key) != current.get(key)
        }
        if not changed_fields:
            continue
        changed.append(
            {
                "ticker": ticker,
                "changed_fields": sorted(changed_fields),
                "from": {key: base.get(key) for key in sorted(changed_fields)},
                "to": {key: current.get(key) for key in sorted(changed_fields)},
            }
        )
    return changed
