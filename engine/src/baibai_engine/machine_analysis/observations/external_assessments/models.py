"""Immutable received observations; no analysis or I/O."""

from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Any, Literal, Self
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict, Field, FiniteFloat, field_validator, model_validator

from baibai_engine.primitives.stocks import normalize_ticker

CONTRACT_VERSION: Literal["1"] = "1"
VALUATION_METRICS = ("consensus_target", "per_company_forecast", "per_consensus", "pbr")
QUANTS_METRICS = (
    "growth_score",
    "undervaluation_score",
    "size_score",
    "technical_score",
    "financial_health_score",
    "market_trend_score",
)


def utc_datetime(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("observation and cutoff times must be timezone-aware")
    return value.astimezone(UTC)


class FrozenModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ReceivedObservation(FrozenModel):
    ticker: str
    fetched_at_utc: datetime
    snapshot_date: date
    distributor: str = Field(min_length=1)
    origin: str | None = None
    source_url: str = Field(min_length=1)
    parser_version: str = Field(min_length=1)
    contract_version: Literal["1"] = CONTRACT_VERSION

    @model_validator(mode="before")
    @classmethod
    def observed_date(cls, data: Any) -> Any:
        if isinstance(data, dict) and "snapshot_date" not in data and "fetched_at_utc" in data:
            received = data["fetched_at_utc"]
            if isinstance(received, str):
                received = datetime.fromisoformat(received)
            if isinstance(received, datetime):
                data = {
                    **data,
                    "snapshot_date": utc_datetime(received)
                    .astimezone(ZoneInfo("Asia/Tokyo"))
                    .date(),
                }
        return data

    @field_validator("ticker")
    @classmethod
    def valid_ticker(cls, value: str) -> str:
        return normalize_ticker(value)

    @field_validator("fetched_at_utc")
    @classmethod
    def aware_time(cls, value: datetime | None) -> datetime | None:
        return utc_datetime(value) if value is not None else None

    @model_validator(mode="after")
    def check_received_date(self) -> Self:
        if self.snapshot_date != self.fetched_at_utc.astimezone(ZoneInfo("Asia/Tokyo")).date():
            raise ValueError("snapshot_date must be the JST received date")
        # Timestamp fields on subclasses have the same aware-time contract.
        for name in type(self).model_fields:
            if name.endswith("_updated_at_utc"):
                value = getattr(self, name)
                if value is not None:
                    object.__setattr__(self, name, utc_datetime(value))
        return self


class ModelValuationObservation(ReceivedObservation):
    consensus_target: FiniteFloat | None = None
    per_company_forecast: FiniteFloat | None = None
    per_consensus: FiniteFloat | None = None
    pbr: FiniteFloat | None = None
    currency: str | None = None
    unit: str | None = None
    share_basis: str | None = None
    consensus_target_origin: str | None = None
    consensus_target_period: str | None = None
    consensus_target_count: int | None = Field(default=None, ge=0, strict=True)
    consensus_target_updated_at_utc: datetime | None = None
    consensus_target_updated_label: str | None = None
    per_company_forecast_origin: str | None = None
    per_company_forecast_period: str | None = None
    per_company_forecast_count: int | None = Field(default=None, ge=0, strict=True)
    per_company_forecast_updated_at_utc: datetime | None = None
    per_company_forecast_updated_label: str | None = None
    per_consensus_origin: str | None = None
    per_consensus_period: str | None = None
    per_consensus_count: int | None = Field(default=None, ge=0, strict=True)
    per_consensus_updated_at_utc: datetime | None = None
    per_consensus_updated_label: str | None = None
    pbr_origin: str | None = None
    pbr_period: str | None = None
    pbr_count: int | None = Field(default=None, ge=0, strict=True)
    pbr_updated_at_utc: datetime | None = None
    pbr_updated_label: str | None = None


class AnalystRatingObservation(ReceivedObservation):
    rating: FiniteFloat | None = None
    rating_scale_min: FiniteFloat | None = None
    rating_scale_max: FiniteFloat | None = None
    rating_scale_direction: Literal["higher_is_positive", "lower_is_positive"] | None = None
    rating_count: int | None = Field(default=None, ge=0, strict=True)
    rating_updated_at_utc: datetime | None = None
    rating_updated_label: str | None = None

    @model_validator(mode="after")
    def rating_in_scale(self) -> Self:
        low, high = self.rating_scale_min, self.rating_scale_max
        if low is not None and high is not None:
            if low >= high:
                raise ValueError("rating scale must have increasing bounds")
            if self.rating is not None and not low <= self.rating <= high:
                raise ValueError("rating outside declared scale")
        return self


class QuantsScoreObservation(ReceivedObservation):
    """Provider's quantitative scores; independent of human analyst consensus."""

    distributor: Literal["kabumap"] = "kabumap"
    origin: Literal["Quants Research"] = "Quants Research"
    score_as_of_date: date
    score_scale_max: Literal[10] = 10
    composite_scale_max: Literal[100] = 100
    growth_score: FiniteFloat | None = Field(default=None, ge=0, le=10)
    undervaluation_score: FiniteFloat | None = Field(default=None, ge=0, le=10)
    size_score: FiniteFloat | None = Field(default=None, ge=0, le=10)
    technical_score: FiniteFloat | None = Field(default=None, ge=0, le=10)
    financial_health_score: FiniteFloat | None = Field(default=None, ge=0, le=10)
    market_trend_score: FiniteFloat | None = Field(default=None, ge=0, le=10)
    composite_score: FiniteFloat | None = Field(default=None, ge=0, le=100)
    industry_average_score: FiniteFloat | None = Field(default=None, ge=0, le=100)


class AnalystForecastObservation(ReceivedObservation):
    """Visible Minkabu analyst fields, separate from its combined/crowd prediction."""

    distributor: Literal["minkabu"] = "minkabu"
    availability: Literal["available", "not_covered"] = "available"
    display_as_of_date: date | None = None
    rating_label: str | None = None
    strong_buy_count: int | None = Field(default=None, ge=0, strict=True)
    buy_count: int | None = Field(default=None, ge=0, strict=True)
    neutral_count: int | None = Field(default=None, ge=0, strict=True)
    sell_count: int | None = Field(default=None, ge=0, strict=True)
    strong_sell_count: int | None = Field(default=None, ge=0, strict=True)
    target_price_yen: FiniteFloat | None = Field(default=None, ge=0)
    target_horizon_months: Literal[12] | None = None
    forecast_period_label: str | None = None
    analyst_forecast_as_of_date: date | None = None
    company_forecast_as_of_date: date | None = None
    analyst_revenue_million_yen: FiniteFloat | None = None
    analyst_net_income_million_yen: FiniteFloat | None = None
    analyst_eps_yen: FiniteFloat | None = None
    company_revenue_million_yen: FiniteFloat | None = None
    company_net_income_million_yen: FiniteFloat | None = None
    company_eps_yen: FiniteFloat | None = None
    share_basis: str | None = None

    @model_validator(mode="after")
    def consistent_availability(self) -> Self:
        if self.availability == "available":
            if self.display_as_of_date is None:
                raise ValueError("available forecast requires its displayed date")
        else:
            forecast_fields = (
                type(self).model_fields.keys() - ReceivedObservation.model_fields.keys()
            )
            if any(getattr(self, name) is not None for name in forecast_fields - {"availability"}):
                raise ValueError("not-covered forecast cannot carry forecast values or dates")
        return self


type Observation = (
    ModelValuationObservation
    | AnalystRatingObservation
    | QuantsScoreObservation
    | AnalystForecastObservation
)
