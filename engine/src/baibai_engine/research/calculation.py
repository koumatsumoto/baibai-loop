"""Generate existing draft number fields from independently supplied typed inputs."""

from __future__ import annotations

from datetime import date
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from baibai_engine.valuation.models import Valuation
from baibai_engine.valuation.scenario import finite_decimal, maximum_entry_price, project_return

from .assessment import DerivedMetric, InputSnapshot, _date, _tuple, ratio_value_from_facts
from .decimal_number import decimal_to_number

_CONFIG = ConfigDict(frozen=True, strict=True, extra="forbid", allow_inf_nan=False)


class RatioInput(BaseModel):
    """The existing ratio-v1 definition without a handwritten result."""

    model_config = _CONFIG
    metric_id: Annotated[str, Field(min_length=1)]
    unit: Annotated[str, Field(min_length=1)]
    as_of: date
    formula: Literal["ratio"] = "ratio"
    formula_version: Literal["ratio-v1"] = "ratio-v1"
    input_fact_ids: tuple[Annotated[str, Field(min_length=1)], Annotated[str, Field(min_length=1)]]
    assumption: Annotated[str, Field(min_length=1)]
    source_ids: tuple[Annotated[str, Field(min_length=1)], ...]

    @field_validator("as_of", mode="before")
    @classmethod
    def _as_of(cls, value: object) -> date:
        return _date(value)

    @field_validator("input_fact_ids", "source_ids", mode="before")
    @classmethod
    def _sequences(cls, value: object) -> object:
        return _tuple(value)


class RatioInputs(BaseModel):
    model_config = _CONFIG
    metrics: tuple[RatioInput, ...] = ()

    @field_validator("metrics", mode="before")
    @classmethod
    def _metrics(cls, value: object) -> object:
        return _tuple(value)


class CalculationInput(BaseModel):
    model_config = _CONFIG
    input_snapshot: InputSnapshot
    derived: RatioInputs = Field(default_factory=RatioInputs)
    valuation: Valuation | None = None


def calculate(inputs: CalculationInput) -> dict[str, object]:
    """Arithmetic only: no price retrieval, readiness, persistence or order policy."""
    snapshot = inputs.input_snapshot
    facts = {fact.fact_id: fact for fact in snapshot.facts}
    if len(facts) != len(snapshot.facts):
        raise ValueError("fact_id must be unique")
    metrics = inputs.derived.metrics
    if len({metric.metric_id for metric in metrics}) != len(metrics):
        raise ValueError("derived metric_id must be unique")
    results = []
    for metric in metrics:
        value, errors = ratio_value_from_facts(
            metric_id=metric.metric_id,
            input_fact_ids=metric.input_fact_ids,
            unit=metric.unit,
            facts=facts,
        )
        if errors:
            raise ValueError("; ".join(errors))
        assert value is not None
        result = DerivedMetric.model_validate(
            {**metric.model_dump(mode="json"), "value": float(value)}
        )
        results.append(result.model_dump(mode="json"))
    valuation = inputs.valuation
    result_payload: dict[str, object] = {
        "derived": {"metrics": results},
        "valuation": None if valuation is None else valuation.model_dump(mode="json"),
        "recalculated_projections": [],
        "projection_numbers": None,
    }
    if valuation is None or valuation.status == "unresolved":
        return result_payload
    price_fact = facts.get(valuation.market_price_fact_id or "")
    if price_fact is None:
        raise ValueError("valuation price fact does not resolve")
    if (
        price_fact.fact_kind != "market_price"
        or price_fact.unit != "JPY_per_share"
        or price_fact.price_basis not in {"realtime", "last_close_unadjusted"}
        or isinstance(price_fact.value, bool | str)
    ):
        raise ValueError("price requires a numeric unadjusted market_price in JPY_per_share")
    price = finite_decimal(price_fact.value)
    assert valuation.horizon_months is not None
    assert valuation.required_annual_return_pct is not None
    context: dict[str, object] = {
        "price_context": "assessment_snapshot",
        "valuation_as_of": snapshot.as_of.isoformat(),
        "price_yen": decimal_to_number(price),
        "price_as_of": price_fact.as_of.isoformat(),
        "price_basis": price_fact.price_basis,
        "horizon_months": valuation.horizon_months,
        "required_annual_return_pct": decimal_to_number(valuation.required_annual_return_pct),
        "return_basis": "conditional_pretax_without_reinvestment",
    }
    reviewed = []
    for name, projection in (("base", valuation.base), ("downside", valuation.downside)):
        assert projection is not None
        returns = project_return(
            projection, price_yen=price, horizon_months=valuation.horizon_months
        )
        context[name] = {
            "total_value_yen": decimal_to_number(returns.total_value_yen),
            "total_return_pct": decimal_to_number(returns.total_return_pct),
            "annualized_return_pct": decimal_to_number(returns.annualized_return_pct),
            "pmax_raw_yen": decimal_to_number(
                maximum_entry_price(
                    projection,
                    horizon_months=valuation.horizon_months,
                    required_annual_return_pct=valuation.required_annual_return_pct,
                )
            ),
        }
        reviewed.append(
            {
                "name": name,
                "terminal_value_per_share_yen": projection.model_dump(mode="json")[
                    "terminal_value_per_share_yen"
                ],
                "cash_distribution_per_share_yen": projection.model_dump(mode="json")[
                    "cash_distribution_per_share_yen"
                ],
            }
        )
    result_payload["recalculated_projections"] = reviewed
    result_payload["projection_numbers"] = context
    return result_payload
