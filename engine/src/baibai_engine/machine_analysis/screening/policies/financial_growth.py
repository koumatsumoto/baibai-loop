"""保存した成長の観測から、通常探索で使う二つの調査経路を区別する。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Literal

from pydantic import JsonValue

from baibai_engine.machine_analysis.generation.contracts import GeneratedModel, GenerationMetadata
from baibai_engine.machine_analysis.models.company_growth.models import (
    FINANCIAL_SECTOR_INAPPLICABLE,
    Metric,
    Observation,
)
from baibai_engine.machine_analysis.models.company_growth.reading import (
    CompanyGrowthReader,
    metric_value,
    same_comparison_endpoints,
)
from baibai_engine.machine_analysis.screening.policies.economic_path_inputs import (
    _can_use_full_fy,
    _change,
    read_policy_growth,
)


@dataclass(frozen=True, slots=True)
class GrowthDiscovery:
    state: Literal["research_candidate", "input_unresolved", "not_selected"]
    matched_paths: tuple[str, ...]
    missing: tuple[str, ...]


type _Check = Literal["met", "not_met", "missing", "not_applicable"]
_ROUTES = {
    "profit_growth": (
        "positive_net_assets",
        "positive_annual_operating_profit",
        "positive_quarter_operating_profit",
        "quarter_sales_increase",
        "quarter_operating_profit_increase",
        "balance_quarter_basis",
        "annual_quarter_basis",
        "growth_comparison_interval",
        "full_fy_without_newer_income_reversal",
    ),
    "operating_recovery": (
        "positive_net_assets",
        "quarter_operating_profit_observed",
        "quarter_operating_margin_observed",
        "quarter_operating_profit_increase",
        "quarter_operating_margin_increase",
        "balance_quarter_basis",
        "recovery_comparison_interval",
    ),
}


def read_growth_discovery(
    ticker: str, *, models: tuple[GeneratedModel, ...], metadata: GenerationMetadata
) -> tuple[GrowthDiscovery, CompanyGrowthReader]:
    if any(model.subject != ticker for model in models):
        raise ValueError("growth discovery requires one matching subject")
    relevant = [
        model for model in models if model.model_id in {"company_financials", "company_growth"}
    ]
    if len({model.model_id for model in relevant}) != len(relevant):
        raise ValueError("growth discovery requires unique saved models")
    growth = next((model for model in relevant if model.model_id == "company_growth"), None)
    # F/G share the source's exact saved keys. An unrelated old operand is not a
    # normal discovery input; explicit full-company reads retain their own contract.
    reader = read_policy_growth(growth)
    if reader.model is not None and reader.model.ticker != ticker:
        raise ValueError("growth payload belongs to a different subject")
    anchor = reader.latest_period
    latest_ttm = reader.observation(anchor, "annual_ttm")
    full_fy = reader.latest_full_fy
    uses_fy = _can_use_full_fy(anchor, latest_ttm, full_fy)
    annual = full_fy if uses_fy else latest_ttm
    quarter = reader.observation(anchor, "quarter")
    reported = reader.observation(anchor, "reported")
    balance = reader.observation(anchor, "balance")
    sales = _change(reader, quarter, "sales")
    profit = _change(reader, quarter, "operating_profit")
    margin = _change(reader, quarter, "operating_margin")
    adverse = uses_fy and any(
        value is not None and value <= 0
        for observation in (quarter, reported)
        for metric in _INCOME_METRICS
        for value in (metric_value(observation, metric, disclosed_by=metadata.as_of),)
    )
    checks: dict[str, _Check] = {
        "positive_net_assets": _metric_check(balance, "net_assets"),
        "positive_annual_operating_profit": _metric_check(
            annual, "operating_profit", disclosed_by=metadata.as_of if uses_fy else None
        ),
        "positive_quarter_operating_profit": _metric_check(quarter, "operating_profit"),
        "quarter_sales_increase": _change_check(sales),
        "quarter_operating_profit_increase": _change_check(profit),
        "quarter_operating_margin_increase": _change_check(margin),
        "quarter_operating_profit_observed": _metric_check(
            quarter, "operating_profit", positive=False
        ),
        "quarter_operating_margin_observed": _metric_check(
            quarter, "operating_margin", positive=False
        ),
        "balance_quarter_basis": _basis_check(balance, quarter, same_end=True),
        "annual_quarter_basis": _basis_check(annual, quarter, same_end=not uses_fy),
        "growth_comparison_interval": _interval_check(
            sales, profit, cohort_bound=reader.cohort_bound
        ),
        "recovery_comparison_interval": _interval_check(
            profit, margin, cohort_bound=reader.cohort_bound
        ),
        "full_fy_without_newer_income_reversal": "not_met" if adverse else "met",
    }
    states = {route: {checks[name] for name in names} for route, names in _ROUTES.items()}
    matched = tuple(route for route, values in states.items() if values == {"met"})
    if matched:
        return GrowthDiscovery("research_candidate", matched, ()), reader
    # 非該当の条件は、追加の入力を取得しても成立しない。
    if all("not_met" in values or "not_applicable" in values for values in states.values()):
        return GrowthDiscovery("not_selected", (), ()), reader
    # In an unresolved company, an input can be missing even on the other,
    # already-failed route. A sector N/A is not a failed data acquisition.
    missing = tuple(sorted(name for name, state in checks.items() if state == "missing"))
    return GrowthDiscovery("input_unresolved", (), missing), reader


_INCOME_METRICS: tuple[Metric, ...] = ("operating_profit", "operating_margin", "parent_profit")


def _metric_check(
    observation: Observation | None,
    metric: Metric,
    *,
    positive: bool = True,
    disclosed_by: date | None = None,
) -> _Check:
    if observation is None or observation.period_status in {
        "matching_period_not_observed",
        "accounting_periods_incomparable",
    }:
        return "missing"
    value = metric_value(observation, metric, disclosed_by=disclosed_by)
    if value is None:
        item = observation.metrics.get(metric)
        return (
            "not_applicable"
            if item is not None
            and item.value is None
            and item.reason == FINANCIAL_SECTOR_INAPPLICABLE
            else "missing"
        )
    return "met" if not positive or value > 0 else "not_met"


def _change_check(change: dict[str, JsonValue]) -> _Check:
    value = change.get("difference")
    if not isinstance(value, (float, int)) or isinstance(value, bool):
        return (
            "not_applicable" if change.get("reason") == FINANCIAL_SECTOR_INAPPLICABLE else "missing"
        )
    # A nonpositive denominator can invalidate the rate, not the observed difference.
    return "met" if value > 0 else "not_met"


def _basis_check(left: Observation | None, right: Observation | None, *, same_end: bool) -> _Check:
    aligned = (
        left is not None
        and right is not None
        and left.statement_basis in {"consolidated", "nonconsolidated"}
        and left.statement_basis == right.statement_basis
        and left.accounting_standard is not None
        and left.accounting_standard == right.accounting_standard
        and (not same_end or left.period_end == right.period_end)
    )
    return "met" if aligned else "missing"


def _interval_check(
    a: dict[str, JsonValue], b: dict[str, JsonValue], *, cohort_bound: bool
) -> _Check:
    return "met" if same_comparison_endpoints(a, b, cohort_bound=cohort_bound) else "missing"
