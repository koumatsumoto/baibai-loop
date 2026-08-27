from __future__ import annotations

from typing import Any

_READING_INPUT_ID = "reading-2026-07-19"
_SERIES_INPUT_ID = "us-10y"
_FX_INPUT_ID = "usd-jpy"
_SNAPSHOT_INPUT_ID = "snapshot-market-2026-07-19"


def macro_context_payload(
    *,
    context_id: str = "macro-context-2026-07-19-base",
    as_of: str = "2026-07-19",
    published_at: str = "2026-07-19T12:00:00+09:00",
    scorecard_deadline: str = "2026-10-31",
    strategy_layer: bool = True,
) -> dict[str, Any]:
    # ``strategy_layer=False`` models a report written before the integrated layer
    # (synthesis / scenario probabilities / bargain topography / estimate caveats).
    source_ids = [_SERIES_INPUT_ID]
    core = [
        _core_section(
            "regime_summary",
            [_SERIES_INPUT_ID, _READING_INPUT_ID],
            change_since_previous=(
                "比較可能なpublished contextはなく、このrevisionが比較基準になる。"
            ),
            previous_scorecard_review="前回レポートがないため採点対象はない。",
        ),
        _core_section(
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
        _core_section("growth_demand", source_ids),
        _core_section("inflation_costs", source_ids),
        _core_section("liquidity_credit", source_ids),
        # fx and japan also examine the currency, so a dominant force that names either
        # of them can be backed by a series distinct from the shared us.10y.
        _core_section("fx", [*source_ids, _FX_INPUT_ID], series_ids=["us.10y", "usd_jpy"]),
        _core_section("japan", [*source_ids, _FX_INPUT_ID], series_ids=["us.10y", "usd_jpy"]),
        _core_section("valuation", source_ids),
        _core_section(
            "risk_environment",
            source_ids,
            risk_environment={
                "stance": "neutral",
                "confidence": "medium",
                "summary": "金利水準が高止まりする限り、リスクを取る対価は限定的である。",
                "falsifiers": ["10年金利が明確に低下し実質金利も緩む"],
                "source_ids": source_ids,
            },
            scenarios=_scenarios(
                source_ids, deadline=scorecard_deadline, with_probabilities=strategy_layer
            ),
        ),
        _core_section(
            "monitoring",
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
    payload: dict[str, Any] = {
        "schema_version": 4,
        "kind": "macro-context",
        "context_id": context_id,
        "as_of": as_of,
        "published_at": published_at,
        "summary": "金利上昇を注視する。",
        "inputs": {
            "articles": [],
            "indicator_series": [
                {
                    "input_id": _SERIES_INPUT_ID,
                    "provider": "fred",
                    "series_id": "us.10y",
                    "window": "2026-07-01/2026-07-17",
                    "observation_as_of": "2026-07-17",
                    "published_at": "2026-07-17T16:00:00-04:00",
                    "accessed_at": published_at,
                    "status": "ok",
                    "used_for": "長期金利と割引率経路の確認",
                },
                {
                    "input_id": _FX_INPUT_ID,
                    "provider": "fred",
                    "series_id": "usd_jpy",
                    "window": "2026-07-01/2026-07-17",
                    "observation_as_of": "2026-07-17",
                    "published_at": "2026-07-17T16:00:00-04:00",
                    "accessed_at": published_at,
                    "status": "ok",
                    "used_for": "円水準と輸入コスト経路の確認",
                },
            ],
            "reading_snapshots": [
                {
                    "input_id": _READING_INPUT_ID,
                    "rules_revision": "2026-07-25T000000+0900",
                    "reading_asof": as_of,
                    "accessed_at": published_at,
                    "status": "ok",
                    "used_for": "全系列の水準・方向・percentileの確認",
                }
            ],
            "machine_snapshots": [
                {
                    "input_id": _SNAPSHOT_INPUT_ID,
                    "command": "baibai-engine screening market-snapshot",
                    "snapshot_asof": as_of,
                    "observation_as_of": as_of,
                    "accessed_at": published_at,
                    "status": "ok",
                    "used_for": "市場内部とバーゲン地形の確認",
                }
            ],
        },
        "core": core,
        "connection": {
            "section_id": "japan_equity_loop",
            "series_ids": ["us.10y"],
            "core_section_ids": ["rates_policy"],
            "fact_summary": [
                {"summary": "米国10年金利は日本株の割引率にも及ぶ。", "source_ids": source_ids},
                {
                    "summary": "benchmark 20d は横ばいで breadth は中立である。",
                    "source_ids": [_SNAPSHOT_INPUT_ID],
                },
            ],
            "judgment": {
                "summary": "金利感応度の高い企業はresearchで耐性を先に確認する。",
                "direction": "mixed",
                "confidence": "medium",
                "source_ids": source_ids,
            },
            "research_priority_hints": [
                {
                    "summary": "借換需要の大きい企業を先に確認する",
                    "applies_to": "有利子負債が大きく短期借換比率の高い候補",
                    "source_ids": source_ids,
                }
            ],
            "sector_tilts": [
                {
                    "sector": "不動産",
                    "direction": "adverse",
                    "summary": "高duration株は慎重に読む",
                    "source_ids": source_ids,
                }
            ],
            "sizing_cautions": [
                {
                    "severity": "medium",
                    "summary": "金利感応度が高い提案では共通riskを明示する。",
                    "source_ids": source_ids,
                }
            ],
        },
    }
    if strategy_layer:
        payload["synthesis"] = macro_synthesis_payload([*source_ids, _FX_INPUT_ID])
        payload["connection"]["bargain_topography"] = {
            "summary": "割安は全面安ではなく金利敏感セクターの取り残しに出やすい。",
            "source_ids": [_SNAPSHOT_INPUT_ID],
        }
        payload["connection"]["estimate_caveats"] = [
            {
                "summary": "金利上昇局面ではFVアンカーの割引率前提が甘くなりやすい。",
                "applies_to": "有利子負債が大きく金利感応度の高い候補",
                "affected_component": "fv_anchor",
                "materiality": "medium",
                "source_ids": source_ids,
            }
        ]
    return payload


def macro_synthesis_payload(source_ids: list[str] | None = None) -> dict[str, Any]:
    # Each force assigns a distinct series to each named section (rates_policy /
    # valuation lend us.10y, japan / fx lend usd_jpy), which the document validator
    # requires of a cross-channel claim.
    sources = source_ids or [_SERIES_INPUT_ID, _FX_INPUT_ID]
    return {
        "dominant_forces": [
            {
                "force_id": "rates-repricing",
                "title": "長期金利の切り上がり",
                "summary": "政策よりも長期側の金利が上がり、割引率が全資産に効いている。",
                "transmission": "米長期金利の上昇が日本の金利と割引率へ波及する。",
                "core_section_ids": ["rates_policy", "japan"],
                "series_ids": ["us.10y", "usd_jpy"],
                "counter_evidence": "実質金利が反転低下すれば力は減衰する。",
                "direction": "adverse",
                "confidence": "medium",
                "source_ids": sources,
            },
            {
                "force_id": "fx-extreme",
                "title": "為替の極値",
                "summary": "円の水準が分布の端にあり、反転時の速度が非対称になっている。",
                "transmission": "為替が輸出採算とバリュエーションの緩衝へ同時に効く。",
                "core_section_ids": ["fx", "valuation"],
                "series_ids": ["usd_jpy", "us.10y"],
                "counter_evidence": "投機ポジションが中立へ戻れば非対称性は解ける。",
                "direction": "mixed",
                "confidence": "medium",
                "source_ids": sources,
            },
        ],
        "interactions": [
            {
                "summary": "金利と為替が同時に極値にあるため、反転局面では同時に痛む。",
                "force_ids": ["rates-repricing", "fx-extreme"],
                "source_ids": sources,
            }
        ],
    }


def _scenarios(
    source_ids: list[str], *, deadline: str, with_probabilities: bool = True
) -> list[dict[str, Any]]:
    cases = (
        (
            "base",
            "mixed",
            0.5,
            "金利は高止まりする。",
            "10年金利が現行レンジで推移する",
            "企業別の金利耐性が問われ続ける",
            ("at_or_above", 3.5),
        ),
        (
            "bear",
            "adverse",
            0.3,
            "金利が一段上昇する。",
            "10年金利がレンジ上限を超える",
            "借換負担の大きい企業の資金調達が細る",
            ("at_or_above", 5.0),
        ),
        (
            "bull",
            "supportive",
            0.2,
            "金利が低下する。",
            "10年金利がレンジ下限を割る",
            "需要改善とvaluation余地が戻る",
            ("below", 3.0),
        ),
    )
    return [
        {
            "case": case,
            "direction": direction,
            **({"probability": probability} if with_probabilities else {}),
            "summary": summary,
            "conditions": [condition],
            "scorecard": [
                {
                    "series_id": "us.10y",
                    "comparison": comparison,
                    "threshold": threshold,
                    "deadline": deadline,
                },
                {
                    "series_id": "us.10y",
                    "comparison": comparison,
                    "threshold": threshold + 0.25,
                    "deadline": deadline,
                },
            ],
            "economic_implications": [implication],
            "source_ids": source_ids,
        }
        for case, direction, probability, summary, condition, implication, (
            comparison,
            threshold,
        ) in cases
    ]


def _core_section(
    section_id: str,
    source_ids: list[str],
    *,
    series_ids: list[str] | None = None,
    change_since_previous: str | None = None,
    previous_scorecard_review: str | None = None,
    previous_scorecard_snapshot_id: str | None = None,
    material_deltas: list[dict[str, Any]] | None = None,
    risk_environment: dict[str, Any] | None = None,
    scenarios: list[dict[str, Any]] | None = None,
    monitoring_points: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    return {
        "section_id": section_id,
        "series_ids": series_ids or ["us.10y"],
        "fact_summary": [{"summary": "米国10年金利を確認した。", "source_ids": source_ids}],
        "judgment": {
            "summary": "割引率環境は中立から逆風寄りである。",
            "direction": "mixed",
            "confidence": "medium",
            "source_ids": source_ids,
        },
        "economic_connection": {
            "summary": "資金調達コストと需要の両経路へ伝わる。",
            "source_ids": source_ids,
        },
        "change_since_previous": change_since_previous,
        "previous_scorecard_review": previous_scorecard_review,
        "previous_scorecard_snapshot_id": previous_scorecard_snapshot_id,
        "material_deltas": material_deltas or [],
        "risk_environment": risk_environment,
        "scenarios": scenarios or [],
        "monitoring_points": monitoring_points or [],
    }
