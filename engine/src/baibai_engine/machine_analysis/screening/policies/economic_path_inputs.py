"""固定モデルから経済経路の観測を投影する。企業価値の仮定は作らない。"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import replace
from datetime import date, timedelta
from math import isclose

from pydantic import JsonValue

from baibai_engine.machine_analysis.generation.contracts import GeneratedModel, GenerationMetadata
from baibai_engine.machine_analysis.models.company_financials.earnings import read_earnings_price
from baibai_engine.machine_analysis.models.company_growth.models import (
    Change,
    Metric,
    Observation,
    PeriodState,
)
from baibai_engine.machine_analysis.models.company_growth.reading import (
    CompanyGrowthReader,
    metric_value,
)
from baibai_engine.machine_analysis.observations.financials.financial_periods import (
    is_annual_period,
)
from baibai_engine.machine_analysis.screening.policies.economic_path_context import (
    _external_context,
    _growth_coverage,
    _number,
    _object,
    _text,
)
from baibai_engine.machine_analysis.screening.policies.economic_paths import (
    CompanyCoordinates,
    PathCoordinates,
)

_PATH_METRICS: tuple[Metric, ...] = (
    "sales",
    "operating_profit",
    "operating_margin",
    "parent_profit",
)
_INCOME_METRICS: tuple[Metric, ...] = ("operating_profit", "operating_margin", "parent_profit")


def project_company(
    ticker: str, *, models: Sequence[GeneratedModel], metadata: GenerationMetadata
) -> CompanyCoordinates:
    index = {model.model_id: model for model in models}
    payloads = {key: model.payload for key, model in index.items()}
    valuation = payloads.get("company_valuation", {})
    financial = _object(valuation.get("financial"))
    earnings = _object(valuation.get("earnings_price"))
    earnings_read = read_earnings_price(earnings) if earnings else None
    growth = payloads.get("company_growth", {})
    growth_read = CompanyGrowthReader(payloads.get("company_growth"))
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
    anchor = growth_read.latest_period
    annual = growth_read.observation(anchor, "annual_ttm")
    quarter = growth_read.observation(anchor, "quarter")
    reported = growth_read.observation(anchor, "reported")
    full_fy = growth_read.latest_full_fy
    use_full_fy = _can_use_full_fy(anchor, annual, full_fy)
    annual_variant = "full_fy" if use_full_fy else "annual_ttm"
    annual_observations: dict[str, Observation | None] = {"annual_ttm": annual}
    if full_fy is not None:
        annual_observations["full_fy"] = full_fy
    changes: dict[str, dict[str, dict[str, JsonValue]]] = {
        variant: {
            metric: _change(
                growth_read,
                observation,
                metric,
                disclosed_by=metadata.as_of if variant == "full_fy" else None,
            )
            for metric in _PATH_METRICS
        }
        for variant, observation in {"quarter": quarter, **annual_observations}.items()
    }
    # A historical FY cannot restore positive earning power after observed newer weakness.
    full_fy_income_adverse = any(
        value is not None and value <= 0
        for metric in _INCOME_METRICS
        for value in (
            metric_value(quarter, metric, disclosed_by=metadata.as_of),
            metric_value(reported, metric, disclosed_by=metadata.as_of),
        )
    )
    earnings_period = earnings_read.profit_period if earnings_read is not None else (None, None)
    paths = []
    for variant, observation in annual_observations.items():
        fy = variant == "full_fy"
        profit_yield = (
            _ratio(metric_value(observation, "parent_profit", disclosed_by=metadata.as_of), cap)
            if fy
            else y
        )
        margin = metric_value(
            observation, "operating_margin", disclosed_by=metadata.as_of if fy else None
        )
        income_aligned = observation is not None and (
            observation.statement_basis == "consolidated"
            if fy
            else earnings_period == (observation.period_start, observation.period_end)
            and earnings_read is not None
            and earnings_read.statement_basis in {"consolidated", "nonconsolidated"}
            and earnings_read.statement_basis == observation.statement_basis
            and earnings_read.accounting_standard is not None
            and earnings_read.accounting_standard == observation.accounting_standard
        )
        paths.append(
            PathCoordinates(
                path_id="earning_power",
                variant=variant,
                price_value=profit_yield,
                business_value=margin,
                price_group=f"market:reported_{variant}_earnings",
                business_group=f"{sector_group}:{variant}_margin",
                eligible=variant == annual_variant
                and income_aligned
                and _is_positive(profit_yield)
                and _is_positive(margin)
                and (not fy or not full_fy_income_adverse),
                evidence={
                    "earnings_components": None if fy else earnings.get("profit_components"),
                    "earnings_basis": (
                        "reported_parent_profit" if fy else valuation.get("earnings_basis")
                    ),
                    "earnings_period_start": _date_text(observation.period_start)
                    if fy and observation is not None
                    else _date_text(earnings_period[0]),
                    "earnings_period_end": _date_text(observation.period_end)
                    if fy and observation is not None
                    else _date_text(earnings_period[1]),
                    "profit_observation": _observation(
                        growth_read.saved_observation(observation), "parent_profit"
                    )
                    if fy
                    else None,
                    "business_observation": _observation(
                        growth_read.saved_observation(observation), "operating_margin"
                    ),
                    "same_period_and_statement_basis": income_aligned,
                    "selected_variant": annual_variant,
                    "newer_earning_weakness": full_fy_income_adverse if fy else None,
                },
            )
        )
    # Do not escape an observed deterioration through a missing companion metric.
    quarter_sales = changes["quarter"]["sales"]
    quarter_sales_difference = _number(quarter_sales.get("difference"))
    expansion_variant = (
        "quarter"
        if _number(quarter_sales.get("rate")) is not None
        or (quarter_sales_difference is not None and quarter_sales_difference <= 0)
        else annual_variant
    )
    quarter_deterioration = any(
        value is not None and value <= 0
        for value in (
            _number(changes["quarter"][metric].get("difference"))
            for metric in ("operating_profit", "operating_margin")
        )
    )
    recovery_variant = (
        "quarter"
        if _recovery_available(changes["quarter"]) or quarter_deterioration
        else annual_variant
    )
    for variant, observation in {"quarter": quarter, **annual_observations}.items():
        sales_variant = annual_variant if variant == "quarter" else variant
        sales_observation = annual_observations[sales_variant]
        sales = metric_value(
            sales_observation,
            "sales",
            disclosed_by=metadata.as_of if sales_variant == "full_fy" else None,
        )
        sales_price = _ratio(sales, cap)
        change = changes[variant]
        sales_change = change["sales"]
        rate = _number(sales_change.get("rate"))
        paths.append(
            PathCoordinates(
                path_id="business_expansion",
                variant=variant,
                price_value=sales_price,
                business_value=rate,
                price_group=f"{sector_group}:{sales_variant}_sales_price",
                business_group=f"{sector_group}:{variant}:sales_growth",
                eligible=variant == expansion_variant
                and _is_positive(sales_price)
                and _is_positive(rate),
                evidence={
                    "sales_observation": _observation(
                        growth_read.saved_observation(sales_observation), "sales"
                    ),
                    "sales_change": sales_change,
                    "change_observation": _observation(
                        growth_read.saved_observation(observation), "sales"
                    ),
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
                path_id="operating_recovery",
                variant=variant,
                price_value=recovery_price,
                business_value=margin_improvement,
                price_group=f"{sector_group}:{variant}:operating_change_price",
                business_group=f"{sector_group}:{variant}:margin_change",
                eligible=variant == recovery_variant
                and _recovery_available(change)
                and _is_positive(improvement)
                and _is_positive(margin_improvement)
                and _is_positive(recovery_price),
                evidence={
                    "operating_change": op_change,
                    "margin_change": margin_change,
                    "operating_observation": _observation(
                        growth_read.saved_observation(observation), "operating_profit"
                    ),
                    "margin_observation": _observation(
                        growth_read.saved_observation(observation), "operating_margin"
                    ),
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
            path_id="capital_productivity",
            variant="reported_profit_latest_book",
            price_value=book_price,
            business_value=implied_profit_book,
            price_group=f"{sector_group}:book_price",
            business_group=f"{sector_group}:reported_profit_book",
            eligible=book_price is not None and _is_positive(implied_profit_book),
            evidence={
                "pbr": pbr,
                "earnings_yield": y,
                "same_current_market_cap": capital_pricing_aligned,
                "book_amount_implied_by_pbr": _ratio(financial_cap, pbr),
                "earnings_components": earnings.get("profit_components"),
                "book_provenance": "individual_book_source_scope_and_measurement_date_not_exposed",
                "interpretation": (
                    "reported_profit_to_model_implied_book_"
                    "not_verified_common_or_average_equity_roe"
                ),
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
    saved_annual = growth_read.saved_observation(annual)
    saved_full_fy = growth_read.saved_observation(full_fy)
    context: dict[str, JsonValue] = {
        "generation_id": metadata.generation_id,
        "related_subjects": [] if definition is None else list(definition.related_subjects),
        "price_date": earnings.get("price_date"),
        "expected_price_date": valuation.get("expected_price_date"),
        "market_cap_yen": cap,
        "pricing_valid": pricing_valid,
        "latest_growth_period": _date_text(anchor.period_end) if anchor is not None else None,
        "annual_observation": {
            "selected_variant": annual_variant,
            "full_fy_observation": saved_full_fy,
            "full_fy_period_start": saved_full_fy.get("period_start"),
            "full_fy_period_end": saved_full_fy.get("period_end"),
            "full_fy_age_days": (
                (metadata.as_of - full_fy.period_end).days if full_fy is not None else None
            ),
            "latest_ttm_period_status": saved_annual.get("period_status"),
            "latest_ttm_metrics": saved_annual.get("metrics"),
            "latest_reported_observation": (
                growth_read.saved_observation(reported) if reported is not None else None
            ),
            "latest_quarter_observation": growth_read.saved_observation(quarter),
            "capital_fallback": "not_used_without_same_fy_book_provenance",
        },
        "turnover_20d_oku": _object(market.get("price")).get("avg_turnover_20d_oku"),
        "turnover_observation_date": _object(market.get("price")).get("resolved_date"),
        "growth_comparison_basis": growth.get("comparison_basis"),
        "growth_status": growth.get("status"),
        "growth_reason": growth.get("reason"),
        "growth_coverage": _growth_coverage(_object(growth.get("coverage"))),
        "latest_growth_changes": {
            "sales": {
                "latest_quarter": changes["quarter"]["sales"],
                "selected_variant": expansion_variant,
                "selected_comparison": changes[expansion_variant]["sales"],
            },
            "operating_profit": {
                "latest_quarter": changes["quarter"]["operating_profit"],
                "selected_variant": recovery_variant,
                "selected_comparison": changes[recovery_variant]["operating_profit"],
            },
            "operating_margin": {
                "latest_quarter": changes["quarter"]["operating_margin"],
                "selected_variant": recovery_variant,
                "selected_comparison": changes[recovery_variant]["operating_margin"],
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
        ticker=ticker,
        name=_text(master.get("name")),
        sector=sector,
        paths=tuple(paths),
        context=context,
        model_revisions={key: model.calculation_revision for key, model in index.items()},
        not_generated=tuple(model for model in expected if model not in index),
    )


def _can_use_full_fy(
    anchor: PeriodState | None, annual: Observation | None, full_fy: Observation | None
) -> bool:
    if (
        anchor is None
        or annual is None
        or full_fy is None
        or annual.statement_basis != full_fy.statement_basis
        or annual.accounting_standard is None
        or annual.accounting_standard != full_fy.accounting_standard
    ):
        return False
    # Legacy models have no interval status: never infer one from missing metric operands.
    if annual.period_status != "matching_period_not_observed":
        return False
    current_start = full_fy.period_end + timedelta(days=1)
    return (
        anchor.period_start == current_start
        and anchor.fiscal_year_end is not None
        and is_annual_period(current_start, anchor.fiscal_year_end)
    )


def _full_fy_change(
    growth: CompanyGrowthReader,
    observation: Observation,
    change: Change,
    as_of: date,
) -> dict[str, JsonValue]:
    saved = growth.saved_change(change)
    if change.difference is None:
        return saved
    prior = growth.comparison_prior(observation, change)
    if (
        metric_value(observation, change.metric, disclosed_by=as_of) is None
        or not prior
        or any(metric_value(item, change.metric, disclosed_by=as_of) is None for item in prior)
    ):
        return {
            **saved,
            "from_value": None,
            "to_value": None,
            "difference": None,
            "rate": None,
            "margin_points": None,
            "sign_change": None,
            "reason": "comparison_sources_not_observed_by_as_of",
        }
    return saved


def _positive(value: JsonValue | None) -> float | None:
    number = _number(value)
    return number if _is_positive(number) else None


def _is_positive(value: float | None) -> bool:
    return value is not None and value > 0


def _ratio(numerator: float | None, denominator: float | None) -> float | None:
    return (
        _number(numerator / denominator)
        if numerator is not None and denominator is not None and denominator > 0
        else None
    )


def _date_text(value: date | None) -> str | None:
    return value.isoformat() if value is not None else None


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
    growth: CompanyGrowthReader,
    observation: Observation | None,
    metric: Metric,
    *,
    disclosed_by: date | None = None,
) -> dict[str, JsonValue]:
    matches = growth.year_over_year(observation, metric)
    if not matches or observation is None:
        return {}
    saved = growth.saved_change(matches[0])
    if any(growth.saved_change(item) != saved for item in matches[1:]):
        return {
            "metric": metric,
            "measurement": observation.measurement,
            "comparison": "year_over_year",
            "to_start": observation.period_start.isoformat(),
            "to_end": observation.period_end.isoformat(),
            "reason": "ambiguous_growth_comparison",
            "matching_comparison_count": len(matches),
        }
    if disclosed_by is not None:
        return _full_fy_change(growth, observation, matches[0], disclosed_by)
    return saved


def _recovery_available(changes: dict[str, dict[str, JsonValue]]) -> bool:
    op = changes["operating_profit"]
    margin = changes["operating_margin"]
    dates = ("from_start", "from_end", "to_start", "to_end")
    return (
        _number(op.get("difference")) is not None
        and _number(margin.get("difference")) is not None
        and all(op.get(key) is not None and op.get(key) == margin.get(key) for key in dates)
    )
