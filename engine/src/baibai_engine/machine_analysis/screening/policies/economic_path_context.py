"""保存payloadの読取と、候補に添える有限の補足観測。追加scoreは作らない。"""

from __future__ import annotations

from math import isfinite

from pydantic import JsonValue


def _external_context(payload: dict[str, JsonValue]) -> dict[str, JsonValue]:
    tradingview = _object(payload.get("tradingview"))
    comparisons: list[JsonValue] = []
    for kind in ("model_valuations", "analyst_ratings", "quants_scores"):
        for assessment in _objects(payload.get(kind)):
            comparisons.extend(_external_comparisons(kind, assessment))
    comparisons.extend(_external_comparisons("tradingview", tradingview))
    minkabu = _object(_object(payload.get("minkabu")).get("latest"))
    return {
        "method_version": payload.get("method_version"),
        "tradingview_availability": tradingview.get("availability"),
        "comparisons": comparisons,
        "minkabu_latest": {
            key: minkabu.get(key)
            for key in (
                "availability",
                "display_as_of_date",
                "origin",
                "source_url",
                "rating_label",
                "target_price_yen",
                "target_horizon_months",
                "forecast_period_label",
                "analyst_forecast_as_of_date",
                "company_forecast_as_of_date",
                "share_basis",
                "analyst_revenue_million_yen",
                "company_revenue_million_yen",
                "analyst_net_income_million_yen",
                "company_net_income_million_yen",
                "analyst_eps_yen",
                "company_eps_yen",
            )
        }
        if minkabu
        else None,
        "origin_groups": payload.get("origin_groups"),
        "detail_required_for_original_estimates": True,
    }


def _external_comparisons(kind: str, assessment: dict[str, JsonValue]) -> list[JsonValue]:
    inputs = _object(assessment.get("inputs"))
    latest, previous = _object(inputs.get("latest")), _object(inputs.get("previous"))
    comparisons = _objects(assessment.get("comparisons"))
    if kind == "analyst_ratings":
        comparison = _object(assessment.get("comparison"))
        comparisons = (comparison,) if comparison else ()
    summaries: list[JsonValue] = []
    for comparison in comparisons:
        metric = _text(comparison.get("metric"))
        summaries.append(
            {
                "kind": kind,
                "distributor": latest.get("distributor", assessment.get("distributor")),
                **{
                    key: comparison.get(key)
                    for key in (
                        "metric",
                        "state",
                        "comparable",
                        "reason",
                        "latest_count",
                        "previous_count",
                    )
                },
                **{
                    key: _number(comparison.get(key))
                    for key in (
                        "latest_value",
                        "previous_value",
                        "delta",
                    )
                },
                "origin": latest.get(f"{metric}_origin", latest.get("origin")),
                "previous_origin": previous.get(f"{metric}_origin", previous.get("origin")),
                "target_period": latest.get(f"{metric}_period"),
                "previous_target_period": previous.get(f"{metric}_period"),
                "source_url": latest.get("source_url"),
                "fetched_at_utc": latest.get("fetched_at_utc"),
                "score_as_of_date": latest.get("score_as_of_date"),
                "basis": {
                    key: latest[key]
                    for key in (
                        "currency",
                        "unit",
                        "share_basis",
                        "rating_scale_min",
                        "rating_scale_max",
                        "rating_scale_direction",
                        "score_scale_max",
                        "composite_scale_max",
                    )
                    if latest.get(key) is not None
                },
                # TradingView's row label is not a confirmed period for each forecast metric.
                "source_target_period_label": latest.get("target_period_label"),
                "source_target_period_end": latest.get("target_period_end"),
            }
        )
    return summaries


def _object(value: JsonValue | None) -> dict[str, JsonValue]:
    return value if isinstance(value, dict) else {}


def _objects(value: JsonValue | None) -> tuple[dict[str, JsonValue], ...]:
    return (
        tuple(item for item in value if isinstance(item, dict)) if isinstance(value, list) else ()
    )


def _number(value: JsonValue | None) -> float | None:
    return (
        float(value)
        if isinstance(value, (int, float)) and not isinstance(value, bool) and isfinite(value)
        else None
    )


def _text(value: JsonValue | None) -> str | None:
    return value if isinstance(value, str) else None


def _growth_coverage(coverage: dict[str, JsonValue]) -> dict[str, JsonValue]:
    summary = {
        key: coverage.get(key)
        for key in (
            "status",
            "reason",
            "observed_start",
            "observed_end",
            "excluded_document_count",
        )
    }
    for field in ("observed_period_ends", "gaps_between_observations"):
        values = coverage.get(field)
        summary[f"{field}_count"] = len(values) if isinstance(values, list) else None
    return summary
