from __future__ import annotations

from typing import Any


def macro_context_payload(
    *,
    context_id: str = "macro-context-2026-07-19-base",
    as_of: str = "2026-07-19",
    valid_until: str = "2026-08-19",
    published_at: str = "2026-07-19T12:00:00+09:00",
) -> dict[str, Any]:
    source_ids = ["us-10y"]
    sections = [
        _section("regime_summary", source_ids),
        _section(
            "rates_policy",
            source_ids,
            material_deltas=[
                {
                    "channel": "discount_rate",
                    "direction": "adverse",
                    "materiality": "medium",
                    "summary": "長期金利は割引率への上押し圧力になる。",
                    "used_for": "必要利回りの前提確認",
                    "source_ids": source_ids,
                }
            ],
        ),
        _section("growth_demand", source_ids),
        _section("inflation_costs", source_ids),
        _section("fx_liquidity", source_ids),
        _section("japan_specific", source_ids),
        _section(
            "scenarios_connections",
            source_ids,
            investment_connection={
                "summary": "金利感応度の高い企業はresearchで耐性を先に確認する。",
                "sector_tilts": ["高duration株は慎重に読む"],
                "research_priority_hints": ["借換需要の大きい企業を先に確認する"],
                "source_ids": source_ids,
            },
            sizing_cautions=[
                {
                    "severity": "medium",
                    "summary": "金利感応度が高い提案では共通riskを明示する。",
                    "source_ids": source_ids,
                }
            ],
            scenarios=[
                {
                    "case": case,
                    "direction": direction,
                    "summary": summary,
                    "conditions": [condition],
                    "investment_implications": [implication],
                    "source_ids": source_ids,
                }
                for case, direction, summary, condition, implication in (
                    (
                        "base",
                        "mixed",
                        "金利は高止まりする。",
                        "10年金利が現行レンジで推移する",
                        "企業別の金利耐性を確認する",
                    ),
                    (
                        "bear",
                        "adverse",
                        "金利が一段上昇する。",
                        "10年金利がレンジ上限を超える",
                        "借換負担の大きい企業を慎重に扱う",
                    ),
                    (
                        "bull",
                        "supportive",
                        "金利が低下する。",
                        "10年金利がレンジ下限を割る",
                        "需要改善とvaluation余地を再確認する",
                    ),
                )
            ],
        ),
        _section(
            "monitoring_points",
            source_ids,
            monitoring_points=[
                {
                    "event": "米国債市場",
                    "condition": "10年金利が現行レンジを外れる",
                    "view_change": "discount rate判断を更新する",
                    "summary": "金利レンジの離脱を監視する。",
                    "source_ids": source_ids,
                }
            ],
        ),
    ]
    return {
        "schema_version": 3,
        "kind": "macro-context",
        "context_id": context_id,
        "as_of": as_of,
        "valid_until": valid_until,
        "published_at": published_at,
        "summary": "金利上昇を注視する。",
        "inputs": {
            "articles": [],
            "indicator_series": [
                {
                    "input_id": "us-10y",
                    "provider": "fred",
                    "series_id": "us.10y",
                    "window": "2026-07-01/2026-07-17",
                    "observation_as_of": "2026-07-17",
                    "published_at": "2026-07-17T16:00:00-04:00",
                    "accessed_at": published_at,
                    "status": "ok",
                    "used_for": "長期金利と割引率経路の確認",
                }
            ],
        },
        "sections": sections,
    }


def _section(
    section_id: str,
    source_ids: list[str],
    *,
    investment_connection: dict[str, Any] | None = None,
    material_deltas: list[dict[str, Any]] | None = None,
    sizing_cautions: list[dict[str, Any]] | None = None,
    scenarios: list[dict[str, Any]] | None = None,
    monitoring_points: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    return {
        "section_id": section_id,
        "series_ids": ["us.10y"],
        "fact_summary": [{"summary": "米国10年金利を確認した。", "source_ids": source_ids}],
        "judgment": {
            "summary": "割引率環境は中立から逆風寄りである。",
            "direction": "mixed",
            "confidence": "medium",
            "source_ids": source_ids,
        },
        "investment_connection": investment_connection
        or {
            "summary": "企業別の需要と資金調達耐性を確認する。",
            "sector_tilts": [],
            "research_priority_hints": [],
            "source_ids": source_ids,
        },
        "change_since_previous": (
            "比較可能なpublished contextはなく、このrevisionが比較基準になる。"
            if section_id == "regime_summary"
            else None
        ),
        "material_deltas": material_deltas or [],
        "sizing_cautions": sizing_cautions or [],
        "scenarios": scenarios or [],
        "monitoring_points": monitoring_points or [],
    }
