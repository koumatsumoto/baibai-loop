"""Generate existing draft number fields from independently supplied typed inputs."""

from __future__ import annotations

from datetime import date
from decimal import Decimal, localcontext
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from baibai_engine.investment_judgment.company_research.contracts import (
    InputSnapshot,
)
from baibai_engine.investment_judgment.contracts import (
    _date,
    _tuple,
)
from baibai_engine.investment_judgment.stock_assessment.document import (
    DerivedMetric,
    ratio_value_from_facts,
)
from baibai_engine.investment_judgment.stock_assessment.scenario import (
    Projection,
    maximum_entry_price,
    project_return,
    two_case_balance,
)
from baibai_engine.investment_judgment.stock_assessment.valuation import Valuation
from baibai_engine.investment_judgment.stock_assessment.wait_comparison import (
    WaitingTerms,
    waiting_paths,
)
from baibai_engine.primitives.numbers import decimal_to_number, finite_decimal
from baibai_engine.primitives.time import JST

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


class PriceComparisonInput(BaseModel):
    """観測や注文条件とは別の比較価格と、任意の仮数量。"""

    model_config = _CONFIG
    price_yen: Annotated[Decimal, Field(gt=0)]
    hypothetical_quantity: Annotated[int, Field(gt=0)] | None = None
    waiting_terms: WaitingTerms | None = None

    @field_validator("price_yen", mode="before")
    @classmethod
    def _price(cls, value: object) -> Decimal:
        return finite_decimal(value)


class CalculationInput(BaseModel):
    model_config = _CONFIG
    input_snapshot: InputSnapshot
    derived: RatioInputs = Field(default_factory=RatioInputs)
    valuation: Valuation | None = None
    price_comparison: PriceComparisonInput | None = None


def _projection_numbers(
    projection: Projection, *, price: Decimal, months: int, rate: Decimal
) -> dict[str, object]:
    returns = project_return(projection, price_yen=price, horizon_months=months)
    return {
        "total_value_yen": decimal_to_number(returns.total_value_yen),
        "total_return_pct": decimal_to_number(returns.total_return_pct),
        "annualized_return_pct": decimal_to_number(returns.annualized_return_pct),
        "pmax_raw_yen": decimal_to_number(
            maximum_entry_price(projection, horizon_months=months, required_annual_return_pct=rate)
        ),
    }


def _price_comparison(inputs: CalculationInput) -> dict[str, object]:
    comparison, valuation = inputs.price_comparison, inputs.valuation
    assert comparison is not None
    assert valuation is not None
    assert valuation.horizon_months is not None
    assert valuation.required_annual_return_pct is not None
    result: dict[str, object] = {
        "price_context": "hypothetical_entry",
        "valuation_as_of": inputs.input_snapshot.as_of.isoformat(),
        "price_yen": decimal_to_number(comparison.price_yen),
        "horizon_months": valuation.horizon_months,
        "required_annual_return_pct": decimal_to_number(valuation.required_annual_return_pct),
        "return_basis": "conditional_pretax_without_reinvestment",
        "execution_assumption": "full_fill_at_comparison_price",
        "investor_taxes_and_fees_included": False,
        "order_readiness_assessed": False,
    }
    quantity = comparison.hypothetical_quantity
    with localcontext() as arithmetic:
        arithmetic.prec = 50
        if quantity is not None:
            result["hypothetical_quantity"] = quantity
            result["hypothetical_outlay_yen"] = str(comparison.price_yen * quantity)
        for name, projection in (("base", valuation.base), ("downside", valuation.downside)):
            assert projection is not None
            row = _projection_numbers(
                projection,
                price=comparison.price_yen,
                months=valuation.horizon_months,
                rate=valuation.required_annual_return_pct,
            )
            if quantity is not None:
                # 仮の必要資金と損益だけ。口座cash・単元・購入可能量は推定しない。
                total = (
                    projection.terminal_value_per_share_yen
                    + projection.cash_distribution_per_share_yen
                ) * quantity
                row["investment_total_value_yen"] = str(total)
                row["profit_loss_yen"] = str(total - comparison.price_yen * quantity)
            result[name] = row
        assert valuation.base is not None
        assert valuation.downside is not None
        result["two_case_balance"] = two_case_balance(
            valuation.base,
            valuation.downside,
            price_yen=comparison.price_yen,
            horizon_months=valuation.horizon_months,
            required_annual_return_pct=valuation.required_annual_return_pct,
        )
    return result


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
            as_of=metric.as_of,
        )
        if errors:
            raise ValueError("; ".join(errors))
        assert value is not None
        result = DerivedMetric.model_validate(
            {**metric.model_dump(mode="json"), "value": float(value)}
        )
        results.append(result.model_dump(mode="json"))
    valuation = inputs.valuation
    if inputs.price_comparison is not None and (
        valuation is None or valuation.status != "resolved"
    ):
        raise ValueError("price comparison requires a resolved valuation")
    result_payload: dict[str, object] = {
        "derived": {"metrics": results},
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
        "price_observed_at": price_fact.observed_at.isoformat()
        if price_fact.observed_at is not None
        else None,
        "price_basis": price_fact.price_basis,
        "horizon_months": valuation.horizon_months,
        "required_annual_return_pct": decimal_to_number(valuation.required_annual_return_pct),
        "return_basis": "conditional_pretax_without_reinvestment",
    }
    for name, projection in (("base", valuation.base), ("downside", valuation.downside)):
        assert projection is not None
        context[name] = _projection_numbers(
            projection,
            price=price,
            months=valuation.horizon_months,
            rate=valuation.required_annual_return_pct,
        )
    assert valuation.base is not None
    assert valuation.downside is not None
    context["two_case_balance"] = two_case_balance(
        valuation.base,
        valuation.downside,
        price_yen=price,
        horizon_months=valuation.horizon_months,
        required_annual_return_pct=valuation.required_annual_return_pct,
    )
    result_payload["projection_numbers"] = context
    if inputs.price_comparison is not None:
        result_payload["price_comparison_numbers"] = _price_comparison(inputs)
        if terms := inputs.price_comparison.waiting_terms:
            if price_fact.observed_at is not None and (
                price_fact.observed_at.astimezone(JST).date() != price_fact.as_of
                or price_fact.postdates_cited_retrievals(
                    {source.source_id: source for source in snapshot.sources}
                )
            ):
                raise ValueError("waiting reference observation contradicts its date or retrieval")
            result_payload["waiting_paths"] = waiting_paths(
                terms,
                reference_price=price,
                reference_price_as_of=price_fact.as_of,
                limit=inputs.price_comparison.price_yen,
                quantity=inputs.price_comparison.hypothetical_quantity,
                as_of=snapshot.as_of,
                horizon_months=valuation.horizon_months,
                required_annual_return_pct=valuation.required_annual_return_pct,
                base=valuation.base,
                downside=valuation.downside,
            )
    return result_payload
