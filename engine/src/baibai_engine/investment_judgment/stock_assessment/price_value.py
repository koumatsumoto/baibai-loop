"""一株の取得権と当時の観測価格を突き合わせた判断。口座・発注条件は持たない。"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import date
from decimal import Decimal
from typing import TYPE_CHECKING, Annotated, Literal

from pydantic import BaseModel, Field, JsonValue, field_validator, model_validator

from baibai_engine.investment_judgment.contracts import _CONFIG, NonEmptyStr, _date, _tuple
from baibai_engine.primitives.numbers import finite_decimal

if TYPE_CHECKING:
    from baibai_engine.investment_judgment.company_research.contracts import ObservedFact, Source
    from baibai_engine.investment_judgment.stock_assessment.document import StockAssessmentDocument
    from baibai_engine.investment_judgment.stock_assessment.store import ReviewedAssessment


class PriceOnlyCondition(BaseModel):
    """事業・権利を変えない価格による反転。明示上限のない説明を指値に解釈しない。"""

    model_config = _CONFIG
    explanation: NonEmptyStr
    ceiling_yen: Annotated[Decimal, Field(gt=0)] | None

    @field_validator("ceiling_yen", mode="before")
    @classmethod
    def _price(cls, value: object) -> Decimal | None:
        return None if value is None else finite_decimal(value)


class AcquisitionLimit(BaseModel):
    """仮説に不可欠な一時の請求権だけ。残余事業や通常配当の一律期限ではない。"""

    model_config = _CONFIG
    value_fact_id: NonEmptyStr
    last_eligible_trade_date: date | None

    @field_validator("last_eligible_trade_date", mode="before")
    @classmethod
    def _day(cls, value: object) -> date | None:
        return None if value is None else _date(value)


class PriceValueCase(BaseModel):
    model_config = _CONFIG
    status: Literal["supported_at_quote", "not_supported_at_quote", "undetermined"]
    price_compatible_conditions: NonEmptyStr | None
    value_fact_ids: tuple[NonEmptyStr, ...]
    price_only_condition: PriceOnlyCondition | None
    acquisition_limit: AcquisitionLimit | None = Field(default=None, exclude_if=lambda v: v is None)

    @field_validator("value_fact_ids", mode="before")
    @classmethod
    def _facts(cls, value: object) -> object:
        return _tuple(value)

    @model_validator(mode="after")
    def _content(self) -> PriceValueCase:
        if self.status != "undetermined" and (
            self.price_compatible_conditions is None or not self.value_fact_ids
        ):
            raise ValueError("a decided price_value_case requires price conditions and value facts")
        if len(self.value_fact_ids) != len(set(self.value_fact_ids)):
            raise ValueError("price_value_case value_fact_ids must be unique")
        if self.price_only_condition is not None and self.status != "not_supported_at_quote":
            raise ValueError("a price-only condition requires not_supported_at_quote")
        if (
            self.acquisition_limit
            and self.acquisition_limit.value_fact_id not in self.value_fact_ids
        ):
            raise ValueError("acquisition limit must reference a chosen value fact")
        return self


def blank_price_value_case() -> dict[str, JsonValue]:
    # An author must choose even an undetermined outcome. Do not pre-adopt a finding on refresh.
    return {
        "status": None,
        "price_compatible_conditions": None,
        "value_fact_ids": [],
        "price_only_condition": None,
    }


def price_value_errors(
    case: PriceValueCase,
    *,
    as_of: date,
    price: ObservedFact | None,
    facts: Mapping[str, ObservedFact],
    sources: Mapping[str, Source],
) -> list[str]:
    """証拠の座標と型を照合。主張の正しさや受給資格・不確実性の重要度は査読の仕事。"""
    errors: list[str] = []
    if case.status != "undetermined" and price is None:
        errors.append("a decided price_value_case requires an observed ordinary-share price")
    if case.price_compatible_conditions is not None and price is None:
        errors.append("price conditions require an observed price")
    ceiling = None if case.price_only_condition is None else case.price_only_condition.ceiling_yen
    if (
        ceiling is not None
        and price is not None
        and not isinstance(price.value, str | bool)
        and ceiling >= finite_decimal(price.value)
    ):
        errors.append("price-only ceiling must be below the referenced observed price")
    has_primary = False
    for fact_id in case.value_fact_ids:
        fact = facts.get(fact_id)
        if fact is None or fact.fact_kind in {"market_price", "valuation_metric"}:
            errors.append(f"price_value_case requires an independent economic fact: {fact_id}")
            continue
        # A price and an enforceable entitlement can be on different pages of the same filing.
        primary = any(
            ref in sources and sources[ref].source_tier == "primary" for ref in fact.source_ids
        )
        # A share count or a deadline alone does not establish what ordinary owners receive.
        has_primary |= primary and fact.fact_kind not in {
            "shares_outstanding",
            "last_eligible_trade_date",
        }
        limit = case.acquisition_limit
        if (
            fact.fact_kind == "last_eligible_trade_date"
            and (case.status == "supported_at_quote" or case.price_only_condition is not None)
            and (limit is None or limit.value_fact_id != fact_id)
        ):
            errors.append("chosen last eligible trade date must bind its acquisition limit")
        if limit is not None and fact_id == limit.value_fact_id:
            if not primary:
                errors.append("acquisition limit requires a primary source")
            day = limit.last_eligible_trade_date
            if day is None:
                if fact.fact_kind == "last_eligible_trade_date":
                    errors.append("known acquisition date cannot be relabelled unknown")
            elif (
                fact.fact_kind != "last_eligible_trade_date"
                or fact.unit != "date"
                or fact.value != day.isoformat()
            ):
                errors.append("last eligible trade date must match its typed source fact")
            elif day < as_of and (case.status == "supported_at_quote" or case.price_only_condition):
                errors.append(
                    "expired acquisition right cannot support a new buyer or a price condition"
                )
    if case.status != "undetermined" and not has_primary:
        errors.append("a decided price_value_case requires primary common-share value evidence")
    return errors


def referenced_price_fact(document: StockAssessmentDocument) -> ObservedFact | None:
    """作者が選んだ元の観測に限る。代わりの価格を探さない。"""
    price_id = document.valuation.market_price_fact_id
    return next((f for f in document.input_snapshot.facts if f.fact_id == price_id), None)


def reviewed_price_value_case(pair: ReviewedAssessment) -> dict[str, JsonValue] | None:
    """原文を検証済みの一組からだけ投影。古いstance・Pmax・現行quoteで格付けしない。"""
    document = pair.document
    case = document.price_value_case
    if case is None:
        return None
    snapshot = document.input_snapshot
    quote = referenced_price_fact(document)
    return {
        "status": case.status,
        "assessment_as_of": snapshot.as_of.isoformat(),
        "observed_quote": None
        if quote is None
        else {
            "fact_id": quote.fact_id,
            "price_yen": quote.value,
            "as_of": quote.as_of.isoformat(),
            "observed_at": None if quote.observed_at is None else quote.observed_at.isoformat(),
            "price_basis": quote.price_basis,
            "source_ids": list(quote.source_ids),
        },
        "price_compatible_conditions": case.price_compatible_conditions,
        "common_share_reason": document.investment_case.explanation,
        "acquisition_limit": None
        if case.acquisition_limit is None
        else case.acquisition_limit.model_dump(mode="json"),
        "value_fact_ids": list(case.value_fact_ids),
        "invalidation_conditions": list(document.investment_case.invalidation_conditions),
        "price_only_condition": None
        if case.price_only_condition is None
        else case.price_only_condition.model_dump(mode="json"),
    }
