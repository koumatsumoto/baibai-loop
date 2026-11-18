"""固定モデルから経済経路の観測を投影する。企業価値の仮定は作らない。"""

from __future__ import annotations

from dataclasses import replace
from datetime import date, timedelta
from math import isclose, isfinite
from typing import cast

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
from baibai_engine.machine_analysis.screening.policies.economic_paths import (
    CompanyCoordinates,
    PathCoordinates,
)

_PATH_METRICS: tuple[Metric, ...] = (
    "sales",
    "operating_profit",
    "operating_margin",
)
_INCOME_METRICS: tuple[Metric, ...] = ("operating_profit", "operating_margin", "parent_profit")


def read_policy_growth(model: GeneratedModel | None) -> CompanyGrowthReader:
    return CompanyGrowthReader(_growth_policy_inputs(model.payload if model is not None else None))


def _project_economic_paths(
    ticker: str,
    *,
    index: dict[str, GeneratedModel],
    metadata: GenerationMetadata,
    growth_read: CompanyGrowthReader,
) -> tuple[CompanyCoordinates, tuple[dict[str, JsonValue], ...]]:
    payloads = {key: model.payload for key, model in index.items()}
    valuation = payloads.get("company_financials", {})
    financial = _object(valuation.get("financial"))
    earnings = _object(valuation.get("earnings_price"))
    earnings_read = read_earnings_price(earnings) if earnings else None
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
    share_input_gap = (
        sector is not None
        and earnings_read is not None
        and earnings_read.unavailable_reason
        in {
            "missing_capital_basis",
            "indeterminate_treasury_after_later_issued_observation",
            "indeterminate_treasury_after_capital_action",
        }
        and earnings_read.shares_ex_treasury is None
        and earnings_read.market_cap_yen is None
        and _positive(earnings_read.price_yen) is not None
        and _text(valuation.get("expected_price_date")) is not None
        and earnings.get("price_date") == valuation.get("expected_price_date")
    )
    expected_price_date = _date(valuation.get("expected_price_date"))
    stale_price_gap = (
        sector is not None
        and earnings_read is not None
        and earnings_read.unavailable_reason == "stale_price"
        and _positive(earnings_read.price_yen) is not None
        and _positive(earnings_read.shares_ex_treasury) is not None
        and _positive(earnings_read.market_cap_yen) is not None
        and earnings_read.price_date is not None
        and expected_price_date is not None
        and earnings_read.price_date < expected_price_date <= metadata.as_of
    )
    price_input_gap = share_input_gap or stale_price_gap
    input_review_paths: list[tuple[str, str]] = []
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
        profit = (
            metric_value(observation, "parent_profit", disclosed_by=metadata.as_of)
            if fy
            else earnings_read.profit_ttm_yen
            if earnings_read is not None
            and all(item.disclosed_at <= metadata.as_of for item in earnings_read.profit_components)
            else None
        )
        if (
            price_input_gap
            and variant == annual_variant
            and income_aligned
            and _is_positive(profit)
            and _is_positive(margin)
            and (not fy or not full_fy_income_adverse)
        ):
            input_review_paths.append(("earning_power", variant))
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
    for variant in changes:
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
        if (
            price_input_gap
            and variant == expansion_variant
            and _is_positive(sales)
            and _is_positive(rate)
        ):
            input_review_paths.append(("business_expansion", variant))
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
            )
        )
    # 選択した分母の出典があっても、利益と同期間の普通株/平均資本とは限らない。
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
        )
    )
    if sector is None:
        paths = [
            replace(
                item,
                price_value=item.price_value if item.path_id == "earning_power" else None,
                business_value=None,
                eligible=False,
            )
            for item in paths
        ]
    coordinates = CompanyCoordinates(
        ticker=ticker,
        name=_text(master.get("name")),
        sector=sector,
        paths=tuple(paths),
    )
    return coordinates, tuple(
        {
            "path_id": path_id,
            "variant": variant,
            "reason": earnings.get("unavailable_reason"),
            "source_field": "earnings_price.price_date"
            if stale_price_gap
            else "earnings_price.shares_ex_treasury",
            "source_model_id": "company_financials",
            "source_model_revision": index["company_financials"].calculation_revision,
            "generation_id": metadata.generation_id,
            "subject": ticker,
            "interpretation": "stale_price_review_not_current_price_or_financial_clearance"
            if stale_price_gap
            else "share_basis_review_not_price_or_financial_clearance",
            **(
                {
                    "price_date": earnings.get("price_date"),
                    "expected_price_date": valuation.get("expected_price_date"),
                }
                if stale_price_gap
                else {}
            ),
        }
        for path_id, variant in input_review_paths
    )


def _growth_policy_inputs(payload: dict[str, JsonValue] | None) -> dict[str, JsonValue] | None:
    if payload is None:
        return None
    periods = cast(list[dict[str, JsonValue]], payload.get("periods", []))
    latest = periods[-1] if periods else None
    full_fy = next((item for item in reversed(periods) if item["fiscal_period"] == "FY"), None)
    targets = {
        (item["measurement"], item["period_start"], item["period_end"])
        for state in (latest, full_fy)
        if state is not None
        for item in cast(list[dict[str, JsonValue]], state["observations"])
        if item["measurement"] == "annual_ttm"
        or (state is latest and item["measurement"] == "quarter")
    }
    changes = [
        item
        for item in cast(list[dict[str, JsonValue]], payload.get("changes", []))
        if item["metric"] in _PATH_METRICS
        and (item["measurement"], item["to_start"], item["to_end"]) in targets
    ]
    prior = {
        (item["measurement"], item.get("from_start"), item.get("from_end")) for item in changes
    }
    # Only the policy's temporary typed read is bounded. Keep every matching prior
    # and duplicate comparison, plus untouched coverage/PDF evidence and saved bytes.
    return {
        **payload,
        "periods": [
            state
            for state in periods
            if state is latest
            or state is full_fy
            or any(
                (item["measurement"], item["period_start"], item["period_end"]) in prior
                for item in cast(list[dict[str, JsonValue]], state["observations"])
            )
        ],
        "changes": list(changes),
    }


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
    # 既知の悪化や科目欠測を古い好業績で埋めない。TTMの期間自体の未観測だけを補う。
    # 旧モデルの科目欠測から、保存されていない期間statusを推定しない。
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


def _date(value: JsonValue | None) -> date | None:
    try:
        return date.fromisoformat(value) if isinstance(value, str) else None
    except ValueError:
        return None


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


def _object(value: JsonValue | None) -> dict[str, JsonValue]:
    return value if isinstance(value, dict) else {}


def _number(value: JsonValue | None) -> float | None:
    return (
        float(value)
        if isinstance(value, (int, float)) and not isinstance(value, bool) and isfinite(value)
        else None
    )


def _text(value: JsonValue | None) -> str | None:
    return value if isinstance(value, str) else None
