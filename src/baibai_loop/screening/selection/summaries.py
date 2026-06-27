"""Candidate tag and summary rendering for selection payload output."""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping, Sequence

from baibai_loop.foundation.coerce import (
    dedupe_strings,
    int_or,
    mapping_or_empty,
    string_or_none,
    string_sequence,
)

from .lenses import _fast_guard_count, _fast_lens, _long_hold_lens

# 2026-05 retro の運用ルール「Nikkei に 3pt 以上劣後している候補は starter size に
# 限定する」を事前固定の annotation 閾値として機械化する。ranking には使わない。
BENCHMARK_LAGGARD_RELATIVE_20D_MAX = -0.03

# 上場 750 暦日以上なのに直近 750 暦日の bar 本数が population 最大の 80% を
# 下回る銘柄は、自己レンジ / sigma gap が前提にする 3 年履歴に長期ギャップが
# ある(新規上場は short_history_flag 側で扱う)。事前固定の annotation 閾値。
PRICE_HISTORY_GAP_COVERAGE_MIN = 0.8
_PRICE_HISTORY_GAP_MIN_LISTING_SPAN_DAYS = 750


def _long_hold_counts(candidates: Sequence[Mapping[str, object]]) -> dict[str, int]:
    counts: Counter[str] = Counter()
    for candidate in candidates:
        rating = string_or_none(_long_hold_lens(candidate).get("rating")) or "unknown"
        counts[rating] += 1
    return dict(counts)


def _candidate_reason_tags(candidate: Mapping[str, object]) -> list[str]:
    tags: list[str] = []
    if _fast_lens(candidate).get("eligible") is True:
        tags.append("fast_dislocation")
    playbook = string_or_none(candidate.get("selection_playbook"))
    if playbook:
        tags.append(playbook)
    long_hold = _long_hold_lens(candidate)
    rating = string_or_none(long_hold.get("rating"))
    if rating == "high":
        tags.append("long_hold_high")
    elif rating == "medium":
        tags.append("long_hold_medium")
    fast = _fast_lens(candidate)
    confidence = string_or_none(fast.get("confidence"))
    if confidence == "high":
        tags.append("fast_confidence_high")
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
    if candidate.get("suppressed") is True:
        tags.append("suppressed_by_prior_research")
    if string_or_none(candidate.get("next_earnings_date")):
        tags.append("earnings_scheduled")
    if candidate.get("freshness_warnings"):
        tags.append("freshness_warning")
    fast = _fast_lens(candidate)
    if fast.get("stale_fundamental_metrics") is True:
        tags.append("stale_fundamental_metrics")
    return dedupe_strings(tags)


def _selection_candidate_summary(
    candidate: Mapping[str, object], *, rank: int
) -> dict[str, object]:
    fast_lens = _fast_lens(candidate)
    long_hold_lens = _long_hold_lens(candidate)
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
        "price_change_5d": candidate.get("price_change_5d"),
        "price_change_20d": candidate.get("price_change_20d"),
        # dislocation 深度: 売られすぎ度の主要 window。fast/long-hold lens と RR の前提
        "price_change_60d": candidate.get("price_change_60d"),
        "benchmark_relative_20d": candidate.get("benchmark_relative_20d"),
        "gap_from_52w_low": candidate.get("gap_from_52w_low"),
        "price_history_coverage_750d": candidate.get("price_history_coverage_750d"),
        "next_earnings_date": candidate.get("next_earnings_date"),
        "position_tier": candidate.get("position_tier"),
        "fast_confidence": string_or_none(fast_lens.get("confidence")),
        "fast_guard_count": _fast_guard_count(candidate),
        "fast_guard_family_count": int_or(fast_lens.get("fundamental_guard_family_count"), 0),
        "fast_data_status": string_or_none(fast_lens.get("data_status")),
        "long_hold_rating": string_or_none(long_hold_lens.get("rating")),
        "prior_research": candidate.get("prior_research"),
        "previous_candidate": candidate.get("previous_candidate") is True,
        "suppressed": candidate.get("suppressed") is True,
        "suppression_reasons": list(string_sequence(candidate.get("suppression_reasons"))),
        "reason_tags": list(string_sequence(candidate.get("reason_tags"))),
        "risk_tags": list(string_sequence(candidate.get("risk_tags"))),
    }


def _sweep_candidate_summary(candidate: Mapping[str, object], *, rank: int) -> dict[str, object]:
    fast_lens = _fast_lens(candidate)
    long_hold_lens = _long_hold_lens(candidate)
    return {
        "rank": rank,
        "ticker": string_or_none(candidate.get("ticker")),
        "name": string_or_none(candidate.get("name")),
        "selection_playbook": string_or_none(candidate.get("selection_playbook")),
        "benchmark_relative_20d": candidate.get("benchmark_relative_20d"),
        "fast_confidence": string_or_none(fast_lens.get("confidence")),
        "fast_guard_count": _fast_guard_count(candidate),
        "fast_guard_family_count": int_or(fast_lens.get("fundamental_guard_family_count"), 0),
        "fast_data_status": string_or_none(fast_lens.get("data_status")),
        "stale_fundamental_metrics": fast_lens.get("stale_fundamental_metrics") is True,
        "long_hold_rating": string_or_none(long_hold_lens.get("rating")),
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
                "fast_confidence",
                "fast_guard_count",
                "fast_guard_family_count",
                "fast_data_status",
                "long_hold_rating",
                "selection_playbook",
                "rank",
                "stale_fundamental_metrics",
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
