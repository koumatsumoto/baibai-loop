"""企業調査で確認した出典・観測事実・仮説検討。価格の採否とは分ける。"""

from __future__ import annotations

from datetime import date, datetime
from typing import Annotated, Literal

from pydantic import AwareDatetime, BaseModel, Field, field_validator, model_validator

from baibai_engine.investment_judgment.contracts import (
    _CONFIG,
    NonEmptyStr,
    Ticker,
    _date,
    _datetime,
    _tuple,
)


class Source(BaseModel):
    model_config = _CONFIG

    source_id: NonEmptyStr
    ticker: Ticker
    source_tier: Literal["primary", "secondary", "local_data"]
    ref: NonEmptyStr | None = None
    provider: NonEmptyStr | None = None
    dataset: NonEmptyStr | None = None
    retrieved_at: AwareDatetime
    as_of: date
    used_for: NonEmptyStr

    @field_validator("as_of", mode="before")
    @classmethod
    def _parse_date(cls, value: object) -> date:
        return _date(value)

    @field_validator("retrieved_at", mode="before")
    @classmethod
    def _parse_time(cls, value: object) -> datetime:
        return _datetime(value)

    @model_validator(mode="after")
    def _source_locator(self) -> Source:
        if self.source_tier == "local_data":
            if self.ref is not None or self.provider is None or self.dataset is None:
                raise ValueError("local_data source requires provider and dataset and forbids ref")
        elif self.ref is None or not self.ref.startswith("https://"):
            raise ValueError("external source requires an HTTPS ref")
        elif self.provider is not None or self.dataset is not None:
            raise ValueError("external source forbids local provider and dataset")
        return self


class ObservedFact(BaseModel):
    model_config = _CONFIG

    fact_id: NonEmptyStr
    fact_kind: Literal[
        "market_price",
        "valuation_metric",
        "net_income_attributable_to_owners",
        "fcfe",
        "shares_outstanding",
        "other",
    ]
    value: (
        str
        | bool
        | Annotated[int, Field(ge=-(10**30), le=10**30)]
        | Annotated[float, Field(ge=-1e30, le=1e30)]
    )
    unit: NonEmptyStr
    as_of: date
    source_ids: tuple[NonEmptyStr, ...]
    observed_at: AwareDatetime | None = None
    price_basis: (
        Literal[
            "realtime",
            "last_close_adjusted",
            "last_close_unadjusted",
        ]
        | None
    ) = None

    @field_validator("as_of", mode="before")
    @classmethod
    def _parse_date(cls, value: object) -> date:
        return _date(value)

    @field_validator("source_ids", mode="before")
    @classmethod
    def _parse_sources(cls, value: object) -> object:
        return _tuple(value)

    @field_validator("observed_at", mode="before")
    @classmethod
    def _parse_observed_at(cls, value: object) -> datetime | None:
        return None if value is None else _datetime(value)

    @model_validator(mode="after")
    def _market_price_metadata(self) -> ObservedFact:
        if self.fact_kind == "market_price":
            if self.price_basis is None:
                raise ValueError("market_price requires price_basis")
            if self.price_basis == "realtime" and self.observed_at is None:
                raise ValueError("realtime market_price requires observed_at")
        elif self.observed_at is not None or self.price_basis is not None:
            raise ValueError("observed_at and price_basis are reserved for market_price")
        return self


class InputSnapshot(BaseModel):
    model_config = _CONFIG

    snapshot_version: Literal[1]
    producer_model_version: NonEmptyStr
    ticker: Ticker
    company_name: NonEmptyStr
    sector: NonEmptyStr
    common_factors: tuple[NonEmptyStr, ...]
    as_of: date
    sources: tuple[Source, ...]
    facts: tuple[ObservedFact, ...]

    @field_validator("snapshot_version", mode="before")
    @classmethod
    def _strict_version(cls, value: object) -> object:
        if not isinstance(value, int) or isinstance(value, bool):
            raise ValueError("snapshot_version must be an integer")
        return value

    @field_validator("as_of", mode="before")
    @classmethod
    def _parse_date(cls, value: object) -> date:
        return _date(value)

    @field_validator("sources", "facts", "common_factors", mode="before")
    @classmethod
    def _parse_sequences(cls, value: object) -> object:
        return _tuple(value)

    @model_validator(mode="after")
    def _common_factor_shape(self) -> InputSnapshot:
        if len(set(self.common_factors)) != len(self.common_factors):
            raise ValueError("common_factors must not contain duplicates")
        if self.common_factors != tuple(sorted(self.common_factors)):
            raise ValueError("common_factors must be sorted")
        return self


class ValueRealization(BaseModel):
    model_config = _CONFIG
    mechanism: NonEmptyStr
    timing: Literal["known", "uncertain"]
    timing_reason: NonEmptyStr
    delay_risk: NonEmptyStr


class HypothesisCheck(BaseModel):
    model_config = _CONFIG
    statement: NonEmptyStr
    origin: Literal["user", "research"]
    result: Literal["supported", "refuted", "unresolved"]
    reason: NonEmptyStr
    source_ids: tuple[NonEmptyStr, ...]

    @field_validator("source_ids", mode="before")
    @classmethod
    def _sequences(cls, value: object) -> object:
        return _tuple(value)

    @model_validator(mode="after")
    def _resolved_evidence(self) -> HypothesisCheck:
        if self.result != "unresolved" and not self.source_ids:
            raise ValueError("resolved hypothesis requires source references")
        return self


class ResearchFindings(BaseModel):
    model_config = _CONFIG
    business_understanding: NonEmptyStr
    value_gap_explanation: NonEmptyStr
    realization: ValueRealization
    hypotheses: tuple[HypothesisCheck, ...] = ()
    unresolved_questions: tuple[NonEmptyStr, ...] = ()
    source_ids: Annotated[tuple[NonEmptyStr, ...], Field(min_length=1)]

    @field_validator("hypotheses", "unresolved_questions", "source_ids", mode="before")
    @classmethod
    def _sequences(cls, value: object) -> object:
        return _tuple(value)
