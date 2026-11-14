"""未採択の財務・成長足切り案を、固定Generationで比較する純粋な研究診断。

正の純資産と利益成長、または営業回復を調査対象に残す案であり、健全性・
有機成長・買付採否を認定しない。日次政策・保存Selection・Proposalは変更しない。
"""

from __future__ import annotations

from copy import deepcopy
from typing import Literal, cast

from pydantic import JsonValue

from baibai_engine.machine_analysis.generation.contracts import GeneratedModel, GenerationMetadata
from baibai_engine.machine_analysis.models.company_growth.models import Metric, Observation
from baibai_engine.machine_analysis.models.company_growth.reading import (
    CompanyGrowthReader,
    metric_value,
)
from baibai_engine.machine_analysis.screening.policies.economic_path_inputs import project_company

POLICY_ID = "financial-growth-research-trial-v1"
_MODELS = ("company_financials", "company_growth")
_FINANCIAL_CONTEXT = (
    "cash",
    "debt",
    "fcf_ttm",
    "ocf_ttm",
    "ocf_ttm_period",
    "edinet_reported_flows",
    "edinet_reported_balances",
    "edinet_source_doc_id",
    "edinet_source_period_end",
    "forecast_full_year_loss_flag",
    "earnings_forecast_source",
    "net_share_change_yoy",
)
type CheckState = Literal["met", "not_met", "unavailable"]


def evaluate_financial_growth_gate(
    ticker: str, *, models: tuple[GeneratedModel, ...], metadata: GenerationMetadata
) -> dict[str, JsonValue]:
    """Compare one unadopted policy; the caller supplies one fixed subject/generation."""
    if any(model.subject != ticker for model in models):
        raise ValueError("financial-growth diagnostic requires one matching subject")
    relevant = [model for model in models if model.model_id in _MODELS]
    if len({model.model_id for model in relevant}) != len(relevant):
        raise ValueError("financial-growth diagnostic requires unique saved models")
    index = {model.model_id: model for model in relevant}
    growth_model = index.get("company_growth")
    reader = CompanyGrowthReader(growth_model.payload if growth_model is not None else None)
    if reader.model is not None and reader.model.ticker != ticker:
        raise ValueError("growth payload belongs to a different subject")

    # Reuse the owner's bounded FY/comparison projection, not price, path eligibility,
    # peer ranks or TA. Even an unavailable pricing contract is irrelevant here.
    projected = project_company(
        ticker, models=(growth_model,) if growth_model is not None else (), metadata=metadata
    ).context
    annual_context = _object(projected.get("annual_observation"))
    uses_fy = annual_context.get("selected_variant") == "full_fy"
    annual = (
        reader.latest_full_fy if uses_fy else reader.observation(reader.latest_period, "annual_ttm")
    )
    quarter = reader.observation(reader.latest_period, "quarter")
    reported = reader.observation(reader.latest_period, "reported")
    balance = reader.observation(reader.latest_period, "balance")
    changes = {
        metric: _object(_object(projected.get("latest_growth_changes")).get(metric)).get(
            "latest_quarter"
        )
        for metric in ("sales", "operating_profit", "operating_margin")
    }
    checks = {
        "positive_net_assets": _metric_check(balance, "net_assets"),
        "positive_annual_operating_profit": _metric_check(
            annual, "operating_profit", metadata=metadata if uses_fy else None
        ),
        "positive_quarter_operating_profit": _metric_check(quarter, "operating_profit"),
        "quarter_sales_increase": _change_check(changes["sales"]),
        "quarter_operating_profit_increase": _change_check(changes["operating_profit"]),
        "quarter_operating_margin_increase": _change_check(changes["operating_margin"]),
        "quarter_operating_profit_observed": _metric_check(
            quarter, "operating_profit", positive=False
        ),
        "quarter_operating_margin_observed": _metric_check(
            quarter, "operating_margin", positive=False
        ),
        "balance_quarter_basis": _basis_check(balance, quarter, same_end=True),
        "annual_quarter_basis": _basis_check(annual, quarter, same_end=not uses_fy),
        "growth_comparison_interval": _interval_check(
            changes["sales"], changes["operating_profit"]
        ),
        "recovery_comparison_interval": _interval_check(
            changes["operating_profit"], changes["operating_margin"]
        ),
        "full_fy_without_newer_income_reversal": _fy_reversal_check(
            uses_fy, quarter, reported, metadata
        ),
    }
    ordinary = (
        "positive_net_assets",
        "positive_annual_operating_profit",
        "positive_quarter_operating_profit",
        "quarter_sales_increase",
        "quarter_operating_profit_increase",
        "balance_quarter_basis",
        "annual_quarter_basis",
        "growth_comparison_interval",
        "full_fy_without_newer_income_reversal",
    )
    recovery = (
        "positive_net_assets",
        "quarter_operating_profit_observed",
        "quarter_operating_margin_observed",
        "quarter_operating_profit_increase",
        "quarter_operating_margin_increase",
        "balance_quarter_basis",
        "recovery_comparison_interval",
    )
    routes = {
        "profit_growth": _route(ordinary, checks),
        "operating_recovery": _route(recovery, checks),
    }
    matched = [name for name, route in routes.items() if route["state"] == "matched"]
    state = (
        "research_candidate"
        if matched
        else "input_unresolved"
        if any(route["state"] == "unresolved" for route in routes.values())
        else "not_selected"
    )
    financial_model = index.get("company_financials")
    financial = _object(
        financial_model.payload.get("financial") if financial_model is not None else None
    )
    return deepcopy(
        {
            "policy_id": POLICY_ID,
            "adoption_status": "unadopted_research_only",
            "read_semantics": "saved_generation_not_point_in_time_reconstruction",
            "ticker": ticker,
            "generation_id": metadata.generation_id,
            "as_of": metadata.as_of.isoformat(),
            "evaluation_time": metadata.evaluation_time.isoformat(),
            "state": state,
            "matched_routes": list(matched),
            "routes": dict(routes),
            "checks": dict(checks),
            "observations": {
                "annual": reader.saved_observation(annual),
                "annual_variant": "full_fy" if uses_fy else "annual_ttm",
                "quarter": reader.saved_observation(quarter),
                "balance": reader.saved_observation(balance),
                "latest_reported": reader.saved_observation(reported),
                "latest_ttm": reader.saved_observation(
                    reader.observation(reader.latest_period, "annual_ttm")
                ),
                "quarter_changes": changes,
                "growth_status": projected.get("growth_status"),
                "growth_reason": projected.get("growth_reason"),
                "comparison_basis": projected.get("growth_comparison_basis"),
                "coverage": projected.get("growth_coverage"),
            },
            "financial_context": {key: financial.get(key) for key in _FINANCIAL_CONTEXT},
            "missing_models": [name for name in _MODELS if name not in index],
            "model_revisions": {name: model.calculation_revision for name, model in index.items()},
            "limits": [
                "not_financial_safety_or_organic_growth_clearance",
                "financing_cash_conversion_and_dilution_require_l3_judgment",
                "not_selected_is_not_proposal_no_action",
                "unresolved_is_not_proposal_defer",
            ],
        }
    )


def _object(value: JsonValue | None) -> dict[str, JsonValue]:
    return value if isinstance(value, dict) else {}


def _check(state: CheckState, value: JsonValue, reason: str | None) -> dict[str, JsonValue]:
    return {"state": state, "value": value, "reason": reason}


def _metric_check(
    observation: Observation | None,
    metric: Metric,
    *,
    positive: bool = True,
    metadata: GenerationMetadata | None = None,
) -> dict[str, JsonValue]:
    item = observation.metrics.get(metric) if observation is not None else None
    if observation is not None and observation.period_status in {
        "matching_period_not_observed",
        "accounting_periods_incomparable",
    }:
        return _check("unavailable", None, observation.period_status)
    value = metric_value(
        observation, metric, disclosed_by=metadata.as_of if metadata is not None else None
    )
    if value is None:
        reason = item.reason if item is not None else "metric_not_observed"
        if item is not None and item.value is not None and metadata is not None:
            reason = "metric_sources_not_observed_by_as_of"
        return _check("unavailable", None, reason or "metric_not_observed")
    return _check(
        "met" if not positive or value > 0 else "not_met", value, item.reason if item else None
    )


def _change_check(change: JsonValue | None) -> dict[str, JsonValue]:
    saved = _object(change)
    value = saved.get("difference")
    if not isinstance(value, (float, int)) or isinstance(value, bool):
        return _check(
            "unavailable", None, cast(str | None, saved.get("reason")) or "comparison_not_observed"
        )
    # A nonpositive denominator invalidates a percentage, not its observed difference.
    return _check("met" if value > 0 else "not_met", value, cast(str | None, saved.get("reason")))


def _basis_check(
    left: Observation | None, right: Observation | None, *, same_end: bool
) -> dict[str, JsonValue]:
    if left is None or right is None:
        return _check("unavailable", None, "observation_not_observed")
    aligned = (
        left.statement_basis in {"consolidated", "nonconsolidated"}
        and left.statement_basis == right.statement_basis
        and left.accounting_standard is not None
        and left.accounting_standard == right.accounting_standard
        and (not same_end or left.period_end == right.period_end)
    )
    return _check(
        "met" if aligned else "unavailable",
        aligned,
        None if aligned else "basis_or_period_unaligned",
    )


def _interval_check(left: JsonValue | None, right: JsonValue | None) -> dict[str, JsonValue]:
    first, second = _object(left), _object(right)
    aligned = all(
        first.get(key) is not None and first.get(key) == second.get(key)
        for key in ("from_start", "from_end", "to_start", "to_end")
    )
    return _check(
        "met" if aligned else "unavailable",
        aligned,
        None if aligned else "comparison_intervals_unaligned",
    )


def _fy_reversal_check(
    uses_fy: bool,
    quarter: Observation | None,
    reported: Observation | None,
    metadata: GenerationMetadata,
) -> dict[str, JsonValue]:
    adverse = uses_fy and any(
        value is not None and value <= 0
        for observation in (quarter, reported)
        for metric in cast(
            tuple[Metric, ...], ("operating_profit", "operating_margin", "parent_profit")
        )
        for value in (metric_value(observation, metric, disclosed_by=metadata.as_of),)
    )
    return _check(
        "not_met" if adverse else "met", adverse, "newer_income_nonpositive" if adverse else None
    )


def _route(names: tuple[str, ...], checks: dict[str, dict[str, JsonValue]]) -> dict[str, JsonValue]:
    failures = [name for name in names if checks[name]["state"] == "not_met"]
    missing = [name for name in names if checks[name]["state"] == "unavailable"]
    return {
        "state": "not_matched" if failures else "unresolved" if missing else "matched",
        "checks": list(names),
        "failures": list(failures),
        "missing": list(missing),
    }
