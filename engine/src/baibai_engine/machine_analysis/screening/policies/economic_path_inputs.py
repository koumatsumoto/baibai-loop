"""固定モデルから経済経路の観測を投影する。企業価値の仮定は作らない。"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import replace
from datetime import date, timedelta
from math import isclose, isfinite

from pydantic import JsonValue

from baibai_engine.machine_analysis.generation.contracts import GeneratedModel, GenerationMetadata
from baibai_engine.machine_analysis.screening.policies.economic_paths import (
    CompanyCoordinates,
    PathCoordinates,
)


def project_company(
    ticker: str, *, models: Sequence[GeneratedModel], metadata: GenerationMetadata
) -> CompanyCoordinates:
    index = {model.model_id: model for model in models}
    payloads = {key: model.payload for key, model in index.items()}
    valuation = payloads.get("company_valuation", {})
    financial = _object(valuation.get("financial"))
    earnings = _object(valuation.get("earnings_price"))
    growth = payloads.get("company_growth", {})
    market = payloads.get("market_context", {})
    master = _object(market.get("master"))
    sector = _text(master.get("sector_33"))
    if sector is not None and sector.strip() in {"", "0", "その他"}:
        sector = None
    sector_group = sector or "unknown_sector"
    cap = _positive(earnings.get("market_cap_yen"))
    pricing_valid = (
        cap is not None
        and _positive(earnings.get("price_yen")) is not None
        and _positive(earnings.get("shares_ex_treasury")) is not None
        and _text(valuation.get("expected_price_date")) is not None
        and earnings.get("price_date") == valuation.get("expected_price_date")
    )
    if not pricing_valid:
        cap = None
    y = _number(earnings.get("earnings_yield")) if pricing_valid else None
    periods = _objects(growth.get("periods"))
    anchor = max(periods, key=lambda item: str(item.get("period_end", "")), default={})
    observations = {
        _text(item.get("measurement")): item for item in _objects(anchor.get("observations"))
    }
    annual = observations.get("annual_ttm", {})
    quarter = observations.get("quarter", {})
    sales = _metric(annual, "sales")
    margin = _metric(annual, "operating_margin")
    earnings_period = _earnings_period(earnings)
    income_aligned = (
        earnings_period == _period(annual)
        and earnings.get("statement_basis") in {"consolidated", "nonconsolidated"}
        and earnings.get("statement_basis") == annual.get("statement_basis")
        and all(isinstance(value, str) for value in _period(annual))
    )
    paths = [
        PathCoordinates(
            "earning_power",
            "annual_ttm",
            y,
            margin,
            "market:reported_ttm_earnings",
            f"{sector_group}:annual_ttm_margin",
            income_aligned and _is_positive(y) and _is_positive(margin),
            {
                "earnings_components": earnings.get("profit_components"),
                "earnings_basis": valuation.get("earnings_basis"),
                "earnings_period_start": earnings_period[0],
                "earnings_period_end": earnings_period[1],
                "business_observation": _observation(annual, "operating_margin"),
                "same_period_and_statement_basis": income_aligned,
            },
        )
    ]
    changes: dict[str, dict[str, dict[str, JsonValue]]] = {}
    for variant, observation in (("quarter", quarter), ("annual_ttm", annual)):
        changes[variant] = {
            metric: _change(growth, observation, metric)
            for metric in ("sales", "operating_profit", "operating_margin")
        }
    # Availability selects the period before the economic sign or rank is inspected.
    expansion_variant = (
        "quarter" if _number(changes["quarter"]["sales"].get("rate")) is not None else "annual_ttm"
    )
    recovery_variant = "quarter" if _recovery_available(changes["quarter"]) else "annual_ttm"
    sales_price = _ratio(sales, cap)
    for variant, observation in (("quarter", quarter), ("annual_ttm", annual)):
        change = changes[variant]
        sales_change = change["sales"]
        rate = _number(sales_change.get("rate"))
        paths.append(
            PathCoordinates(
                "business_expansion",
                variant,
                sales_price,
                rate,
                f"{sector_group}:annual_ttm_sales_price",
                f"{sector_group}:{variant}:sales_growth",
                variant == expansion_variant and _is_positive(sales_price) and _is_positive(rate),
                {
                    "sales_observation": _observation(annual, "sales"),
                    "sales_change": sales_change,
                    "change_observation": _observation(observation, "sales"),
                    "selected_variant": expansion_variant,
                },
            )
        )
        op_change = change["operating_profit"]
        margin_change = change["operating_margin"]
        improvement = _number(op_change.get("difference"))
        margin_improvement = _number(margin_change.get("difference"))
        recovery_price = _ratio(improvement, cap)
        paths.append(
            PathCoordinates(
                "operating_recovery",
                variant,
                recovery_price,
                margin_improvement,
                f"{sector_group}:{variant}:operating_change_price",
                f"{sector_group}:{variant}:margin_change",
                variant == recovery_variant
                and _recovery_available(change)
                and _is_positive(improvement)
                and _is_positive(margin_improvement)
                and _is_positive(recovery_price),
                {
                    "operating_change": op_change,
                    "margin_change": margin_change,
                    "operating_observation": _observation(observation, "operating_profit"),
                    "margin_observation": _observation(observation, "operating_margin"),
                    "selected_variant": recovery_variant,
                },
            )
        )
    pbr = _positive(financial.get("pbr"))
    financial_cap = _positive(financial.get("market_cap"))
    capital_pricing_aligned = (
        cap is not None
        and financial_cap is not None
        and isclose(cap, financial_cap, rel_tol=1e-12, abs_tol=0)
    )
    book_price = _ratio(1.0, pbr) if capital_pricing_aligned else None
    implied_profit_book = (
        _number(pbr * y) if pbr is not None and y is not None and capital_pricing_aligned else None
    )
    paths.append(
        PathCoordinates(
            "capital_productivity",
            "reported_profit_latest_book",
            book_price,
            implied_profit_book,
            f"{sector_group}:book_price",
            f"{sector_group}:reported_profit_book",
            book_price is not None and _is_positive(implied_profit_book),
            {
                "pbr": pbr,
                "earnings_yield": y,
                "same_current_market_cap": capital_pricing_aligned,
                "book_amount_implied_by_pbr": _ratio(financial_cap, pbr),
                "earnings_components": earnings.get("profit_components"),
                "book_provenance": "individual_book_source_scope_and_measurement_date_not_exposed",
                "interpretation": "reported_profit_to_model_implied_book_not_verified_common_or_average_equity_roe",
            },
        )
    )
    if sector is None:
        paths = [
            replace(
                item,
                price_value=item.price_value if item.path_id == "earning_power" else None,
                business_value=None,
                eligible=False,
                evidence={**item.evidence, "peer_group_unavailable": "sector_not_observed"},
            )
            for item in paths
        ]
    definition = None if metadata.subject_plan is None else metadata.subject_plan.get(ticker)
    expected = () if definition is None else definition.model_ids
    technical = payloads.get("technical_analysis", {})
    external = payloads.get("external_assessment", {})
    context: dict[str, JsonValue] = {
        "generation_id": metadata.generation_id,
        "price_date": earnings.get("price_date"),
        "expected_price_date": valuation.get("expected_price_date"),
        "market_cap_yen": cap,
        "pricing_valid": pricing_valid,
        "latest_growth_period": anchor.get("period_end"),
        "turnover_20d_oku": _object(market.get("price")).get("avg_turnover_20d_oku"),
        "turnover_observation_date": _object(market.get("price")).get("resolved_date"),
        "growth_comparison_basis": growth.get("comparison_basis"),
        "growth_coverage": _growth_coverage(_object(growth.get("coverage"))),
        "latest_growth_changes": {
            "sales": {
                "variant": expansion_variant,
                "comparison": changes[expansion_variant]["sales"],
            },
            "operating_profit": {
                "variant": recovery_variant,
                "comparison": changes[recovery_variant]["operating_profit"],
            },
            "operating_margin": {
                "variant": recovery_variant,
                "comparison": changes[recovery_variant]["operating_margin"],
            },
        },
        "financial": {
            key: financial.get(key)
            for key in (
                "forecast_profit_yen",
                "forecast_ordinary_profit_yen",
                "earnings_forecast_source",
                "forecast_special_gain_flag",
                "forecast_full_year_loss_flag",
                "cash",
                "debt",
                "fcf_ttm",
                "edinet_source_period_start",
                "edinet_source_period_end",
                "edinet_source_doc_id",
                "ttm_quality_fcf_yield",
                "dividend_yield",
                "net_share_change_yoy",
                "tradable_share_change_yoy",
            )
        },
        "price_momentum": technical.get("price_momentum"),
        "rsi_14bar": technical.get("rsi_14bar"),
        "bollinger_20bar": technical.get("bollinger_20bar"),
        "market_relative": market.get("relative"),
        "external_assessment": _external_context(external),
        "path_inputs": [
            {
                "path_id": item.path_id,
                "variant": item.variant,
                "price_value": item.price_value,
                "business_value": item.business_value,
                "eligible": item.eligible,
            }
            for item in paths
        ],
        "full_result_reference": {"generation_id": metadata.generation_id, "subject": ticker},
    }
    return CompanyCoordinates(
        ticker,
        _text(master.get("name")),
        sector,
        tuple(paths),
        context,
        {key: model.calculation_revision for key, model in index.items()},
        tuple(model for model in expected if model not in index),
    )


def _external_context(payload: dict[str, JsonValue]) -> dict[str, JsonValue]:
    tradingview = _object(payload.get("tradingview"))
    comparisons: list[JsonValue] = []
    for kind in ("model_valuations", "analyst_ratings", "quants_scores"):
        for assessment in _objects(payload.get(kind)):
            comparisons.extend(_external_comparisons(kind, assessment))
    comparisons.extend(_external_comparisons("tradingview", tradingview))
    minkabu = _object(_object(payload.get("minkabu")).get("latest"))
    return {
        "status": payload.get("status"),
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


def _positive(value: JsonValue | None) -> float | None:
    number = _number(value)
    return number if _is_positive(number) else None


def _is_positive(value: float | None) -> bool:
    return value is not None and value > 0


def _text(value: JsonValue | None) -> str | None:
    return value if isinstance(value, str) else None


def _ratio(numerator: float | None, denominator: float | None) -> float | None:
    return (
        _number(numerator / denominator)
        if numerator is not None and denominator is not None and denominator > 0
        else None
    )


def _period(observation: dict[str, JsonValue]) -> tuple[JsonValue | None, JsonValue | None]:
    return observation.get("period_start"), observation.get("period_end")


def _metric(observation: dict[str, JsonValue], name: str) -> float | None:
    return _number(_object(_object(observation.get("metrics")).get(name)).get("value"))


def _observation(observation: dict[str, JsonValue], name: str) -> dict[str, JsonValue]:
    return {
        "measurement": observation.get("measurement"),
        "period_start": observation.get("period_start"),
        "period_end": observation.get("period_end"),
        "statement_basis": observation.get("statement_basis"),
        "metric": name,
        "observation": _object(_object(observation.get("metrics")).get(name)),
    }


def _change(
    growth: dict[str, JsonValue], observation: dict[str, JsonValue], metric: str
) -> dict[str, JsonValue]:
    if not observation or not all(isinstance(value, str) for value in _period(observation)):
        return {}
    matches = [
        item
        for item in _objects(growth.get("changes"))
        if item.get("metric") == metric
        and item.get("comparison") == "year_over_year"
        and item.get("measurement") == observation.get("measurement")
        and item.get("to_start") == observation.get("period_start")
        and item.get("to_end") == observation.get("period_end")
    ]
    if len(matches) > 1:
        raise ValueError("ambiguous growth comparison in fixed model")
    return matches[0] if matches else {}


def _recovery_available(changes: dict[str, dict[str, JsonValue]]) -> bool:
    op = changes["operating_profit"]
    margin = changes["operating_margin"]
    dates = ("from_start", "from_end", "to_start", "to_end")
    return (
        _number(op.get("difference")) is not None
        and _number(margin.get("difference")) is not None
        and all(op.get(key) is not None and op.get(key) == margin.get(key) for key in dates)
    )


def _earnings_period(earnings: dict[str, JsonValue]) -> tuple[str | None, str | None]:
    components = _objects(earnings.get("profit_components"))
    positive = [item for item in components if item.get("coefficient") == 1]
    negative = [item for item in components if item.get("coefficient") == -1]
    if len(positive) == 1 and not negative:
        return _text(positive[0].get("period_start")), _text(positive[0].get("period_end"))
    if len(positive) == 2 and len(negative) == 1:
        prior_end = _text(negative[0].get("period_end"))
        endpoints = [_text(item.get("period_end")) for item in positive]
        if prior_end is not None and all(item is not None for item in endpoints):
            return (date.fromisoformat(prior_end) + timedelta(days=1)).isoformat(), max(
                item for item in endpoints if item is not None
            )
    return None, None


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
