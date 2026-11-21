"""固定モデルから経済経路の観測を投影する。企業価値の仮定は作らない。"""

from __future__ import annotations

from datetime import date, timedelta
from math import isclose, isfinite
from typing import cast

from pydantic import JsonValue

from baibai_engine.machine_analysis.generation.contracts import GeneratedModel
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
from baibai_engine.machine_analysis.screening.discovery_contract import EconomicCoordinate

_PATH_METRICS: tuple[Metric, ...] = (
    "sales",
    "operating_profit",
    "operating_margin",
)
_RECOVERY_METRICS: tuple[Metric, ...] = ("operating_profit", "operating_margin")
_INCOME_METRICS: tuple[Metric, ...] = ("operating_profit", "operating_margin", "parent_profit")


def read_policy_growth(model: GeneratedModel | None) -> CompanyGrowthReader:
    return CompanyGrowthReader(_growth_policy_inputs(model.payload if model is not None else None))


def project_economic_coordinates(
    financial_model: dict[str, JsonValue],
    *,
    sector: str | None,
    as_of: date,
    growth: CompanyGrowthReader,
) -> tuple[list[EconomicCoordinate], str | None]:
    financial = _object(financial_model.get("financial"))
    earnings = _object(financial_model.get("earnings_price"))
    earnings_read = read_earnings_price(earnings) if earnings else None
    # 業種不明は比較群ではない。元の財務モデルは照会できるが、座標は公表しない。
    if sector is None:
        return [], None
    cap = _positive(earnings.get("market_cap_yen"))
    expected = financial_model.get("expected_price_date")
    price_on_expected_day = isinstance(expected, str) and earnings.get("price_date") == expected
    pricing_valid = (
        cap is not None
        and _positive(earnings.get("price_yen")) is not None
        and _positive(earnings.get("shares_ex_treasury")) is not None
        and price_on_expected_day
    )
    if not pricing_valid:
        cap = None
    y = _number(earnings.get("earnings_yield")) if pricing_valid else None
    price_input_reason = None
    if earnings_read is not None and _positive(earnings_read.price_yen) is not None:
        share_gap = (
            earnings_read.unavailable_reason
            in {
                "missing_capital_basis",
                "indeterminate_treasury_after_later_issued_observation",
                "indeterminate_treasury_after_capital_action",
            }
            and earnings_read.shares_ex_treasury is None
            and earnings_read.market_cap_yen is None
            and price_on_expected_day
        )
        expected_date = _date(expected)
        stale_price = (
            earnings_read.unavailable_reason == "stale_price"
            and _positive(earnings_read.shares_ex_treasury) is not None
            and _positive(earnings_read.market_cap_yen) is not None
            and earnings_read.price_date is not None
            and expected_date is not None
            and earnings_read.price_date < expected_date <= as_of
        )
        if share_gap or stale_price:
            price_input_reason = earnings_read.unavailable_reason

    anchor = growth.latest_period
    annual = growth.observation(anchor, "annual_ttm")
    full_fy = growth.latest_full_fy
    use_full_fy = _can_use_full_fy(anchor, annual, full_fy)
    annual_variant = "full_fy" if use_full_fy else "annual_ttm"
    if use_full_fy:
        annual = full_fy
    quarter = growth.observation(anchor, "quarter")
    annual_cutoff = as_of if use_full_fy else None

    coordinates: list[EconomicCoordinate] = []
    review_reason = None
    margin = metric_value(annual, "operating_margin", disclosed_by=annual_cutoff)
    if use_full_fy:
        # 古い好業績で、既知の直近の赤字・ゼロを埋めない。
        reported = growth.observation(anchor, "reported")
        adverse = any(
            value is not None and value <= 0
            for metric in _INCOME_METRICS
            for value in (
                metric_value(quarter, metric, disclosed_by=as_of),
                metric_value(reported, metric, disclosed_by=as_of),
            )
        )
        income_aligned = annual is not None and annual.statement_basis == "consolidated"
        profit = metric_value(annual, "parent_profit", disclosed_by=as_of)
        profit_yield = _ratio(profit, cap)
    else:
        adverse = False
        income_aligned = (
            annual is not None
            and earnings_read is not None
            and earnings_read.profit_period == (annual.period_start, annual.period_end)
            and earnings_read.statement_basis in {"consolidated", "nonconsolidated"}
            and earnings_read.statement_basis == annual.statement_basis
            and earnings_read.accounting_standard is not None
            and earnings_read.accounting_standard == annual.accounting_standard
        )
        profit = (
            earnings_read.profit_ttm_yen
            if price_input_reason is not None
            and earnings_read is not None
            and all(item.disclosed_at <= as_of for item in earnings_read.profit_components)
            else None
        )
        profit_yield = y
    if income_aligned and _is_positive(margin) and not adverse:
        if price_input_reason is not None and _is_positive(profit):
            review_reason = price_input_reason
        if _is_positive(profit_yield):
            coordinates.append(
                EconomicCoordinate(
                    path_id="earning_power",
                    variant=annual_variant,
                    price_value=profit_yield,
                    business_value=margin,
                    price_group=f"market:reported_{annual_variant}_earnings",
                    business_group=f"{sector}:{annual_variant}_margin",
                )
            )

    # 既知の四半期悪化を、もう一つの指標の欠測や好調な年次で隠さない。
    quarter_changes = {metric: _change(growth, quarter, metric) for metric in _PATH_METRICS}
    quarter_sales = quarter_changes["sales"]
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
            _number(quarter_changes[metric].get("difference"))
            for metric in ("operating_profit", "operating_margin")
        )
    )
    recovery_variant = (
        "quarter"
        if _recovery_available(quarter_changes) or quarter_deterioration
        else annual_variant
    )
    # 先に採用期間を選び、保存しない期間の座標は計算しない。公開配列は従来の期間順を保つ。
    for variant in ("quarter", annual_variant):
        if variant == expansion_variant:
            sale_change = (
                quarter_sales
                if variant == "quarter"
                else _change(growth, annual, "sales", disclosed_by=annual_cutoff)
            )
            rate = _number(sale_change.get("rate"))
            if _is_positive(rate):
                sales = metric_value(annual, "sales", disclosed_by=annual_cutoff)
                if price_input_reason is not None and _is_positive(sales):
                    review_reason = price_input_reason
                sales_price = _ratio(sales, cap)
                if _is_positive(sales_price):
                    coordinates.append(
                        EconomicCoordinate(
                            path_id="business_expansion",
                            variant=variant,
                            price_value=sales_price,
                            business_value=rate,
                            price_group=f"{sector}:{annual_variant}_sales_price",
                            business_group=f"{sector}:{variant}:sales_growth",
                        )
                    )
        if variant == recovery_variant:
            changes = (
                quarter_changes
                if variant == "quarter"
                else {
                    metric: _change(growth, annual, metric, disclosed_by=annual_cutoff)
                    for metric in _RECOVERY_METRICS
                }
            )
            if _recovery_available(changes):
                improvement = _number(changes["operating_profit"].get("difference"))
                margin_improvement = _number(changes["operating_margin"].get("difference"))
                if _is_positive(improvement) and _is_positive(margin_improvement):
                    recovery_price = _ratio(improvement, cap)
                    if _is_positive(recovery_price):
                        coordinates.append(
                            EconomicCoordinate(
                                path_id="operating_recovery",
                                variant=variant,
                                price_value=recovery_price,
                                business_value=margin_improvement,
                                price_group=f"{sector}:{variant}:operating_change_price",
                                business_group=f"{sector}:{variant}:margin_change",
                            )
                        )

    # 利益と同じ期間の普通株/平均資本を実測したROEではない。
    pbr = _positive(financial.get("pbr"))
    financial_cap = _positive(financial.get("market_cap"))
    if (
        pbr is not None
        and y is not None
        and cap is not None
        and financial_cap is not None
        and isclose(cap, financial_cap, rel_tol=1e-12, abs_tol=0)
    ):
        book_price, implied_profit_book = _ratio(1.0, pbr), _number(pbr * y)
        if book_price is not None and _is_positive(implied_profit_book):
            coordinates.append(
                EconomicCoordinate(
                    path_id="capital_productivity",
                    variant="reported_profit_latest_book",
                    price_value=book_price,
                    business_value=implied_profit_book,
                    price_group=f"{sector}:book_price",
                    business_group=f"{sector}:reported_profit_book",
                )
            )
    return coordinates, review_reason


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


def _recovery_available(changes: dict[Metric, dict[str, JsonValue]]) -> bool:
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
