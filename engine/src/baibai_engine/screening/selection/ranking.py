"""Ranking components: evidence choice, playbook order, strength keys."""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from baibai_engine.foundation.coerce import float_or, metric_map, string_or_none


def _best_selection_evidence(
    evidence_hits: Sequence[Mapping[str, object]],
    *,
    playbook_order: Sequence[str],
) -> tuple[str | None, dict[str, object], tuple[float, ...]]:
    entries = [
        (
            _playbook_order_rank(name, playbook_order),
            _evidence_strength_key(name, metrics),
            name,
            metrics,
        )
        for evidence_hit in evidence_hits
        if (name := string_or_none(evidence_hit.get("name"))) is not None
        for metrics in [metric_map(evidence_hit.get("metrics"))]
    ]
    if not entries:
        return None, {}, (0.0,)
    _, strength_key, name, metrics = min(entries, key=lambda item: (item[0], item[1]))
    return name, metrics, strength_key


def _playbook_order_rank(name: str | None, playbook_order: Sequence[str]) -> int:
    try:
        return playbook_order.index(name or "")
    except ValueError:
        return len(playbook_order)


def _sizing_eligible_evidence_hits(
    evidence_hits: Sequence[Mapping[str, object]],
) -> tuple[Mapping[str, object], ...]:
    return tuple(hit for hit in evidence_hits if _is_sizing_eligible_evidence(hit))


def _is_sizing_eligible_evidence(evidence_hit: Mapping[str, object]) -> bool:
    source_status = evidence_hit.get("source_status")
    if isinstance(source_status, str) and source_status != "ok":
        return False
    return evidence_hit.get("sizing_eligible") is not False


def _evidence_strength_key(name: str, metrics: Mapping[str, object]) -> tuple[float, ...]:
    match name:
        case "valuation-reversion":
            return (
                float_or(metrics.get("condition_a_sector_median_gap"), 1.0),
                float_or(metrics.get("condition_a_self_range_percentile"), 1.0),
                float_or(metrics.get("condition_b_sigma_gap"), 1.0),
                float_or(metrics.get("price_change_60d"), 1.0),
            )
        case "cash-rich-asset-discount":
            # 現金の厚みだけで並べる。簿価に対する割安を第 2 キーに置く案は、順位を実際に
            # 動かす変更なので較正の裏づけが要る。書かれない key を読んで既定値へ落ちる形は
            # 「第 2 キーがある」という見た目だけを作り、同点は ticker 順へ落ちていた。
            return (-float_or(metrics.get("cash_to_market_cap"), 0.0),)
        case "cashflow-yield-discount":
            return (
                -float_or(metrics.get("ocf_yield"), 0.0),
                -float_or(metrics.get("cfo_yoy"), -99.0),
            )
        case "sales-discount-growth":
            operating_profit = float_or(metrics.get("operating_profit"), -1.0)
            return (
                float_or(metrics.get("ps_sector_gap"), 1.0),
                -float_or(metrics.get("sales_yoy"), 0.0),
                0.0 if operating_profit >= 0 else 1.0,
            )
        case _:
            return (0.0,)
