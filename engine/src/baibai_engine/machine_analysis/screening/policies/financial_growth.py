"""保存モデルから財務・成長の調査経路と不足を読む純粋な候補診断。

正の純資産と利益成長、または営業回復を区別する。適用不能・未確定と既知の
非充足を保ち、健全性・有機成長・買付採否は認定しない。採用と容量はcallerが持つ。
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
from baibai_engine.machine_analysis.screening.policies.economic_path_context import _growth_coverage
from baibai_engine.machine_analysis.screening.policies.economic_path_inputs import (
    _can_use_full_fy,
    _change,
    read_policy_growth,
)

POLICY_ID = "financial-growth-research-trial-v1"
_MODELS = ("company_financials", "company_growth")
_COMPARABILITY_REASONS = {
    "accounting_periods_incomparable",
    "accounting_standard_incomparable",
    "statement_basis_incomparable",
    "basis_or_period_unaligned",
    "comparison_intervals_unaligned",
    "ambiguous_growth_comparison",
}
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
_FORECAST_CONTEXT = (
    "forecast_profit_yen",
    "forecast_eps_yen",
    "forecast_ordinary_profit_yen",
    "forecast_full_year_loss_flag",
    "earnings_forecast_source",
)
_DIVIDEND_FORECAST_CONTEXT = ("dps_forecast_annual", "dividend_forecast_source")
_CASH_FLOW_CONTEXT = (
    "edinet_reported_flows",
    "edinet_capex_source",
    "edinet_failure_reasons",
    "cfo",
    "cfo_source",
    "ocf_ttm",
    "ocf_ttm_period",
    "ttm_quality_ocf_yield",
)
_QUARTER_METRICS: tuple[Metric, ...] = ("sales", "operating_profit", "operating_margin")
_SHARE_METRICS: tuple[Metric, ...] = ("parent_profit", "reported_basic_eps", "average_shares")

type CheckState = Literal["met", "not_met", "unavailable"]


def evaluate_financial_growth_gate(
    ticker: str, *, models: tuple[GeneratedModel, ...], metadata: GenerationMetadata
) -> dict[str, JsonValue]:
    """Read one financial policy; the caller supplies one fixed subject/generation."""
    gate, _ = _financial_growth_with_path_inputs(ticker, models=models, metadata=metadata)
    return gate


def _financial_growth_with_path_inputs(
    ticker: str, *, models: tuple[GeneratedModel, ...], metadata: GenerationMetadata
) -> tuple[dict[str, JsonValue], CompanyGrowthReader]:
    if any(model.subject != ticker for model in models):
        raise ValueError("financial-growth diagnostic requires one matching subject")
    relevant = [model for model in models if model.model_id in _MODELS]
    if len({model.model_id for model in relevant}) != len(relevant):
        raise ValueError("financial-growth diagnostic requires unique saved models")
    index = {model.model_id: model for model in relevant}
    growth_model = index.get("company_growth")
    growth = growth_model.payload if growth_model is not None else {}
    reader = CompanyGrowthReader(growth if growth_model is not None else None)
    if reader.model is not None and reader.model.ticker != ticker:
        raise ValueError("growth payload belongs to a different subject")

    # Keep the original saved period keys used by economic paths: decoding dates alone
    # must not promote an unmatched comparison. Prepare this bounded read just once.
    path_growth = read_policy_growth(growth_model)
    path_quarter = path_growth.observation(path_growth.latest_period, "quarter")
    anchor = reader.latest_period
    latest_ttm = reader.observation(anchor, "annual_ttm")
    full_fy = reader.latest_full_fy
    uses_fy = _can_use_full_fy(anchor, latest_ttm, full_fy)
    annual = full_fy if uses_fy else latest_ttm
    quarter = reader.observation(anchor, "quarter")
    reported = reader.observation(anchor, "reported")
    balance = reader.observation(anchor, "balance")
    changes: dict[str, JsonValue | None] = {
        metric: _change(path_growth, path_quarter, metric) for metric in _QUARTER_METRICS
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
    routes["operating_recovery"]["profitability"] = (
        {
            "current_quarter": _profit_sign(checks["quarter_operating_profit_observed"]["value"]),
            "comparison_prior_quarter": _profit_sign(
                _object(changes["operating_profit"]).get("from_value")
            ),
        }
        if routes["operating_recovery"]["state"] == "matched"
        else None
    )
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
    gate: dict[str, JsonValue] = deepcopy(
        {
            "policy_id": POLICY_ID,
            "read_semantics": "saved_generation_not_point_in_time_reconstruction",
            "ticker": ticker,
            "generation_id": metadata.generation_id,
            "as_of": metadata.as_of.isoformat(),
            "evaluation_time": metadata.evaluation_time.isoformat(),
            "state": state,
            "matched_routes": list(matched),
            "routes": dict(routes),
            "checks": dict(checks),
            "income_attribution_context": {
                "read_semantics": "saved_income_not_normalized_or_common_share_cash",
                "annual_variant": "full_fy" if uses_fy else "annual_ttm",
                "metric_units": {
                    key: value
                    for key, value in _object(growth.get("metric_units")).items()
                    if key in {"operating_profit", "parent_profit"}
                },
                "annual": _observation_metrics(
                    reader, annual, ("operating_profit", "parent_profit")
                ),
                "quarter": _observation_metrics(
                    reader, quarter, ("operating_profit", "parent_profit")
                ),
            },
            "reported_share_context": {
                "read_semantics": "reported_basic_eps_not_diluted_or_current_share_basis",
                "comparison_basis": growth.get("comparison_basis"),
                "metric_units": {
                    key: value
                    for key, value in _object(growth.get("metric_units")).items()
                    if key in _SHARE_METRICS
                },
                "reported": _observation_metrics(reader, reported, _SHARE_METRICS),
                "year_over_year_observations": {
                    metric: [
                        reader.saved_change(change)
                        for change in reader.year_over_year(reported, metric)
                    ]
                    for metric in _SHARE_METRICS
                },
            },
            "quarter_growth_context": {
                "comparison_basis": growth.get("comparison_basis"),
                "metric_units": {
                    key: value
                    for key, value in _object(growth.get("metric_units")).items()
                    if key in changes
                },
                "changes": changes,
            },
            "observation_period_context": {
                "read_semantics": "saved_measurement_and_source_dates_not_point_in_time",
                "annual_variant": "full_fy" if uses_fy else "annual_ttm",
                "annual": _period_context(reader, annual),
                "quarter": _period_context(reader, quarter),
                "balance": _period_context(reader, balance),
            },
            "observations": {
                "annual": reader.saved_observation(annual),
                "annual_variant": "full_fy" if uses_fy else "annual_ttm",
                "quarter": reader.saved_observation(quarter),
                "balance": reader.saved_observation(balance),
                "latest_reported": reader.saved_observation(reported),
                "latest_ttm": reader.saved_observation(latest_ttm),
                "quarter_changes": changes,
                "quarter_comparison_prior_observations": _comparison_priors(
                    reader, quarter, changes
                ),
                "growth_status": growth.get("status"),
                "growth_reason": growth.get("reason"),
                "comparison_basis": growth.get("comparison_basis"),
                "coverage": _growth_coverage(_object(growth.get("coverage"))),
            },
            "financial_context": {key: financial.get(key) for key in _FINANCIAL_CONTEXT},
            **(
                {"reported_balance_context": financial["edinet_reported_balances"]}
                if "edinet_reported_balances" in financial
                else {}
            ),
            # 成長/回復の実績とは別の会社予想。未記録やscopeの未確定を補完しない。
            **(
                {
                    "forecast_context": {
                        key: financial[key] for key in _FORECAST_CONTEXT if key in financial
                    },
                    "dividend_forecast_context": {
                        key: financial[key]
                        for key in _DIVIDEND_FORECAST_CONTEXT
                        if key in financial
                    },
                    # 利益の改善と資金化を同一視せず、原CFの期/不足を初回比較へ残す。
                    "cash_flow_context": {
                        key: financial[key] for key in _CASH_FLOW_CONTEXT if key in financial
                    },
                }
                if financial_model is not None
                else {}
            ),
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
    return gate, path_growth


def _comparison_priors(
    reader: CompanyGrowthReader,
    quarter: Observation | None,
    changes: dict[str, JsonValue | None],
) -> list[JsonValue]:
    if quarter is None:
        return []
    priors: dict[int, dict[str, JsonValue]] = {}
    for metric in _QUARTER_METRICS:
        for change in reader.year_over_year(quarter, metric):
            # Follow the already selected comparison, not a second selection rule.
            # PDF-derived priors are not raw observations in periods.observations.
            if change.source_sha256 is not None or reader.saved_change(change) != changes[metric]:
                continue
            for prior in reader.comparison_prior(quarter, change):
                entry = priors.setdefault(
                    id(prior),
                    {"comparison_metrics": [], "observation": reader.saved_observation(prior)},
                )
                metrics = cast(list[JsonValue], entry["comparison_metrics"])
                if metric not in metrics:
                    metrics.append(metric)
    return list(priors.values())


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
    # Saved N/A describes the model's scope, not a failed acquisition. Keep it
    # route-local; it neither vetoes a matched route nor clears a known failure.
    missing_by_kind: dict[str, list[JsonValue]] = {
        "not_applicable": [],
        "unresolved_comparability": [],
        "other_unavailable": [],
    }
    for name in missing:
        reason = checks[name]["reason"]
        kind = (
            "not_applicable"
            if reason == "not_applicable_to_financial_sector"
            else "unresolved_comparability"
            if isinstance(reason, str) and reason in _COMPARABILITY_REASONS
            else "other_unavailable"
        )
        missing_by_kind[kind].append(name)
    return {
        "state": "not_matched" if failures else "unresolved" if missing else "matched",
        "checks": list(names),
        "failures": list(failures),
        "missing": list(missing),
        "missing_by_kind": dict(missing_by_kind),
    }


def _profit_sign(value: JsonValue | None) -> JsonValue:
    if not isinstance(value, (float, int)) or isinstance(value, bool):
        return None
    return "positive" if value > 0 else "negative" if value < 0 else "zero"


def _period_context(
    reader: CompanyGrowthReader, observation: Observation | None
) -> dict[str, JsonValue] | None:
    if observation is None:
        return None
    saved = reader.saved_observation(observation)
    # 全出典日を残し、最大日を観測全体の公表時刻・利用可能時刻にしない。
    dates = set()
    for value in _object(saved.get("metrics")).values():
        documents = _object(value).get("documents")
        if isinstance(documents, list):
            for document in documents:
                day = _object(document).get("disclosed_at")
                if isinstance(day, str):
                    dates.add(day)
    return {
        **{
            key: saved[key]
            for key in (
                "measurement",
                "period_start",
                "period_end",
                "statement_basis",
                "accounting_standard",
                "period_status",
            )
            if key in saved
        },
        "source_disclosure_dates": cast(list[JsonValue], sorted(dates)),
    }


def _observation_metrics(
    reader: CompanyGrowthReader, observation: Observation | None, metrics: tuple[Metric, ...]
) -> dict[str, JsonValue] | None:
    if observation is None:
        return None
    saved = reader.saved_observation(observation)
    return {
        **{
            key: saved[key]
            for key in (
                "measurement",
                "period_start",
                "period_end",
                "statement_basis",
                "accounting_standard",
                "period_status",
            )
            if key in saved
        },
        "metrics": {
            key: value for key, value in _object(saved.get("metrics")).items() if key in metrics
        },
    }
