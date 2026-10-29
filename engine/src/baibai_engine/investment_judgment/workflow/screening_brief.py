"""保存政策を変更せず、初回比較に必要な観測と不足を有限表示する。"""

from __future__ import annotations

import json
from collections import Counter
from copy import deepcopy
from typing import Any


def economic_screening_brief(result: dict[str, Any]) -> dict[str, Any]:
    cards = [_card(card) for card in result["cards"]]
    common_revisions = cards[0].get("model_revisions") if cards else None
    if common_revisions is not None and all(
        card.get("model_revisions") == common_revisions for card in cards
    ):
        for card in cards:
            del card["model_revisions"]
    else:
        common_revisions = None
    return {
        "policy": deepcopy(result["policy"]),
        "eligible_count": result["eligible_count"],
        "cards": cards,
        "common_model_revisions": common_revisions,
        "interpretation": "screening_priority_not_expected_return_or_buy_recommendation",
        "presentation": {
            "kind": "bounded_observation_brief_not_full_saved_policy",
            "external": "same_series_states_not_company_revisions_or_independent_votes",
            "missing": "missing_or_no_comparable_change_is_not_evidence_of_safety_or_stability",
            "growth": "selected_fallbacks_only_when_different_from_latest_quarter",
            "models": "selected_known_model_fields_only_other_generated_models_are_detail_only",
        },
        "detail_read": (
            "selection-context --screening-selection-id <same-id> --compare-ticker <ticker>"
        ),
    }


def _card(card: dict[str, Any]) -> dict[str, Any]:
    context = card["context"]
    financial = _object(context.get("financial"))
    return {
        **_pick(
            card,
            "ticker",
            "name",
            "sector",
            "rank",
            "priority",
            "strongest_path",
            "model_revisions",
            "not_generated",
        ),
        "paths": [
            _path(
                path,
                include_earnings_periods=not any(
                    row.get("path_id") == "earning_power" for row in card["paths"]
                ),
            )
            for path in card["paths"]
        ],
        "inactive_path_inputs": _inactive_paths(card),
        "context": {
            **_pick(
                context,
                "price_date",
                "expected_price_date",
                "market_cap_yen",
                "pricing_valid",
                "latest_growth_period",
                "growth_status",
                "growth_reason",
                "growth_comparison_basis",
                "turnover_20d_oku",
                "turnover_observation_date",
                "related_subjects",
                "full_result_reference",
            ),
            "annual_observation": _annual_observation(_object(context.get("annual_observation"))),
            "growth_coverage": _pick(
                _object(context.get("growth_coverage")),
                "status",
                "reason",
                "observed_start",
                "observed_end",
                "excluded_document_count",
            ),
            "latest_growth_changes": _growth_changes(_object(context.get("latest_growth_changes"))),
            "financial": {
                **_pick(
                    financial,
                    "forecast_profit_yen",
                    "forecast_ordinary_profit_yen",
                    "forecast_special_gain_flag",
                    "forecast_full_year_loss_flag",
                    "cash",
                    "debt",
                    "fcf_ttm",
                    "edinet_ocf_ttm",
                    "edinet_reported_flows",
                    "ocf_ttm_period",
                    "ocf_ttm",
                    "ttm_quality_ocf_yield",
                    "capex_ttm",
                    "edinet_capex_source",
                    "consolidation_basis",
                    "edinet_failure_reasons",
                    "edinet_source_period_start",
                    "edinet_source_period_end",
                    "ttm_quality_fcf_yield",
                    "dividend_yield",
                    "net_share_change_yoy",
                    "tradable_share_change_yoy",
                ),
                "earnings_forecast_source": _pick(
                    _object(financial.get("earnings_forecast_source")),
                    "status",
                    "basis",
                    "target_basis",
                    "period_start",
                    "period_end",
                    "disclosed_at",
                    "fiscal_year_end",
                    "statement_basis",
                    "disclosed_date",
                    "reason",
                ),
            },
            "price_momentum": _pick(
                _object(context.get("price_momentum")),
                "value",
                "status",
                "reason",
                "start_date",
                "end_date",
                "reference_date",
            ),
            "rsi_14bar": _pick(
                _object(context.get("rsi_14bar")),
                "value",
                "status",
                "reason",
                "period_bars",
                "history_end_date",
            ),
            "bollinger_20bar": _pick(
                _object(context.get("bollinger_20bar")),
                "percent_b",
                "position",
                "status",
                "reason",
                "window_start_date",
                "window_end_date",
            ),
            "market_relative": _market_relative(_object(context.get("market_relative"))),
            "external_assessment": _external(_object(context.get("external_assessment"))),
        },
    }


def _annual_observation(annual: dict[str, Any]) -> dict[str, Any]:
    brief = _pick(
        annual,
        "selected_variant",
        "full_fy_period_start",
        "full_fy_period_end",
        "full_fy_age_days",
        "latest_ttm_period_status",
        "capital_fallback",
    )
    if annual.get("selected_variant") == "full_fy":
        for name in ("latest_reported_observation", "latest_quarter_observation"):
            observation = _object(annual.get(name))
            brief[name] = {
                **_pick(observation, "period_start", "period_end", "statement_basis"),
                "metrics": {
                    metric: {
                        **_pick(_object(value), "value", "reason"),
                        "disclosure_dates": sorted(
                            {
                                document["disclosed_at"]
                                for document in _object(value).get("documents", [])
                                if isinstance(document, dict)
                                and isinstance(document.get("disclosed_at"), str)
                            }
                        ),
                    }
                    for metric, value in _object(observation.get("metrics")).items()
                    if metric in {"sales", "operating_profit", "operating_margin", "parent_profit"}
                },
            }
    return brief


def _path(path: dict[str, Any], *, include_earnings_periods: bool) -> dict[str, Any]:
    evidence = _object(path.get("evidence"))
    brief = _pick(
        evidence,
        "earnings_basis",
        "earnings_period_start",
        "earnings_period_end",
        "same_period_and_statement_basis",
        "same_current_market_cap",
        "book_provenance",
        "interpretation",
        "selected_variant",
        "newer_earning_weakness",
        "peer_group_unavailable",
    )
    observations: list[dict[str, Any]] = []
    for name, value in evidence.items():
        if name.endswith("observation"):
            observation = _object(value)
            period = _pick(
                observation, "measurement", "period_start", "period_end", "statement_basis"
            )
            if period not in observations:
                observations.append(period)
        elif name == "earnings_components" and isinstance(value, list) and include_earnings_periods:
            brief["earnings_component_periods"] = [
                _pick(row, "coefficient", "period_start", "period_end")
                for row in value
                if isinstance(row, dict)
            ]
    if observations:
        brief["period_basis"] = observations
    return {
        **_pick(
            path,
            "path_id",
            "variant",
            "score",
            "price_value",
            "business_value",
            "price_percentile",
            "business_percentile",
            "price_population",
            "business_population",
            "price_group",
            "business_group",
        ),
        "evidence": brief,
    }


def _inactive_paths(card: dict[str, Any]) -> list[dict[str, Any]]:
    active = {path.get("path_id") for path in card["paths"]}
    growth = _object(card["context"].get("latest_growth_changes"))
    selected = {
        "business_expansion": _object(growth.get("sales")).get("selected_variant"),
        "operating_recovery": _object(growth.get("operating_profit")).get("selected_variant"),
    }
    return [
        _pick(row, "path_id", "variant", "price_value", "business_value", "eligible")
        for row in card["context"].get("path_inputs", [])
        if row.get("path_id") not in active
        and (row.get("path_id") not in selected or row.get("variant") == selected[row["path_id"]])
    ]


def _growth_changes(changes: dict[str, Any]) -> dict[str, Any]:
    latest, fallbacks, variants = {}, {}, {}
    for metric, value in changes.items():
        row = _object(value)
        quarter = _object(row.get("latest_quarter"))
        selected = _object(row.get("selected_comparison"))
        variants[metric] = row.get("selected_variant")
        latest[metric] = quarter
        if selected != quarter:
            fallbacks[metric] = selected
    return {
        "selected_variants": variants,
        "latest_quarter": _comparison_groups(latest),
        "selected_fallbacks": _comparison_groups(fallbacks),
    }


def _comparison_groups(changes: dict[str, Any]) -> list[dict[str, Any]]:
    groups: dict[tuple[Any, ...], dict[str, Any]] = {}
    for metric, change in changes.items():
        period = _pick(change, "measurement", "from_start", "from_end", "to_start", "to_end")
        key = tuple(period.items())
        group = groups.setdefault(key, {**period, "metrics": {}})
        group["metrics"][metric] = _pick(
            change,
            "from_value",
            "to_value",
            "difference",
            "rate",
            "margin_points",
            "sign_change",
            "reason",
        )
    return list(groups.values())


def _market_relative(relative: dict[str, Any]) -> dict[str, Any]:
    return {
        **_pick(
            relative,
            "benchmark_ticker",
            "relative_1d",
            "relative_5d",
            "relative_20d",
            "relative_60d",
        ),
        "sector": _pick(
            _object(relative.get("sector")),
            "sector_33",
            "peer_count",
            "relative_return_20d",
            "comparison_start_date",
            "comparison_end_date",
        ),
    }


def _external(external: dict[str, Any]) -> dict[str, Any]:
    groups: dict[str, dict[str, Any]] = {}
    changes = []
    states: Counter[str] = Counter()
    for row in external.get("comparisons", []):
        if not isinstance(row, dict):
            continue
        state = str(row.get("state", "unknown"))
        states[state] += 1
        metadata = _pick(
            row,
            "kind",
            "distributor",
            "origin",
            "previous_origin",
            "source_url",
            "fetched_at_utc",
            "score_as_of_date",
            "basis",
            "target_period",
            "previous_target_period",
            "source_target_period_label",
            "source_target_period_end",
        )
        metadata = {
            name: value for name, value in metadata.items() if value is not None or name == "origin"
        }
        key = json.dumps(metadata, sort_keys=True, ensure_ascii=False)
        group = groups.setdefault(
            key,
            {
                **metadata,
                "states": [],
            },
        )
        state_group = next(
            (
                item
                for item in group["states"]
                if item["state"] == state
                and item.get("reason") == row.get("reason")
                and item.get("comparable") == row.get("comparable")
            ),
            None,
        )
        if state_group is None:
            state_group = {
                "state": state,
                "comparable": row.get("comparable"),
                "reason": row.get("reason"),
                "metrics": {},
            }
            group["states"].append(state_group)
        state_group["metrics"][row.get("metric")] = row.get("latest_value")
        if row.get("previous_value") is not None and row.get("previous_value") != row.get(
            "latest_value"
        ):
            state_group.setdefault("previous_values", {})[row.get("metric")] = row["previous_value"]
        if row.get("latest_count") is not None or row.get("previous_count") is not None:
            state_group.setdefault("observation_counts", {})[row.get("metric")] = _pick(
                row, "latest_count", "previous_count"
            )
        if row.get("comparable") is True and state in {"increased", "decreased"}:
            changes.append(
                _pick(
                    row,
                    "kind",
                    "distributor",
                    "origin",
                    "previous_origin",
                    "metric",
                    "state",
                    "comparable",
                    "latest_value",
                    "previous_value",
                    "delta",
                    "latest_count",
                    "previous_count",
                    "reason",
                    "target_period",
                    "previous_target_period",
                    "basis",
                    "source_target_period_label",
                    "source_target_period_end",
                    "fetched_at_utc",
                    "score_as_of_date",
                )
            )
    return {
        **_pick(external, "method_version", "tradingview_availability"),
        "state_counts": dict(states),
        "sources": list(groups.values()),
        "comparable_changes": changes,
        "minkabu": {
            key: deepcopy(value)
            for key, value in _object(external.get("minkabu_latest")).items()
            if key != "source_url" and (value is not None or key in {"origin", "share_basis"})
        },
    }


def _pick(value: dict[str, Any], *names: str) -> dict[str, Any]:
    return {name: deepcopy(value[name]) for name in names if name in value}


def _object(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}
