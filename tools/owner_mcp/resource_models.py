"""調査工程のdata入力を型と範囲で検証し、曖昧な読取要求を止める。"""

from __future__ import annotations

from datetime import date, datetime
from typing import Annotated, Literal

from pydantic import (
    AfterValidator,
    ConfigDict,
    Field,
    JsonValue,
    ValidationInfo,
    field_validator,
    model_validator,
)
from pydantic_core import PydanticCustomError

from tools.l1_mcp.contract import InputModel


def iso_day(value: str) -> str:
    try:
        if date.fromisoformat(value).isoformat() != value:
            raise ValueError("noncanonical date")
    except ValueError as exc:
        raise PydanticCustomError("iso_day", "ISO日付が必要") from exc
    return value


def iso_instant(value: str) -> str:
    try:
        parsed = datetime.fromisoformat(value)
        if parsed.utcoffset() is None:
            raise ValueError("offset required")
    except ValueError as exc:
        raise PydanticCustomError("iso_instant", "timezone付きISO時刻が必要") from exc
    return value


Day = Annotated[str, AfterValidator(iso_day)]
Instant = Annotated[str, AfterValidator(iso_instant)]
Identifier = Annotated[str, Field(min_length=1, max_length=512)]
Ticker = Annotated[str, Field(pattern=r"^[0-9A-Z]{4}$")]


class Empty(InputModel):
    pass


class DateRange(InputModel):
    from_date: Day | None = Field(default=None, alias="from")
    to_date: Day | None = Field(default=None, alias="to")

    @model_validator(mode="after")
    def ordered(self) -> DateRange:
        if self.from_date and self.to_date and self.from_date > self.to_date:
            raise ValueError("from follows to")
        return self


class OneSelector(InputModel):
    @field_validator("*", mode="before")
    @classmethod
    def strict_latest(cls, value: object, info: ValidationInfo) -> object:
        if info.field_name == "latest" and value is not None and type(value) is not bool:
            raise ValueError("latest must be true")
        return value

    @model_validator(mode="after")
    def one(self) -> OneSelector:
        if sum(v is not None for v in self.model_dump().values()) != 1:
            raise ValueError("exactly one selector required")
        return self


class ResourceRef(InputModel):
    resource_id: Identifier
    identity: dict[str, JsonValue]
    sha256: str = Field(pattern=r"^[a-f0-9]{64}$")


class Cursor(InputModel):
    model_config = ConfigDict(allow_inf_nan=False)
    resource_id: Identifier
    filters: dict[str, JsonValue]
    after: list[str | int | float]
    snapshot_token: str | None = None


class SeriesFilters(InputModel):
    category: Identifier | None = None
    geography: Identifier | None = None
    provider: Identifier | None = None


class SeriesKey(InputModel):
    series_id: Identifier


class ObservationFilters(DateRange):
    series_id: Identifier
    from_date: Day = Field(alias="from")
    to_date: Day = Field(alias="to")
    mode: Literal["effective", "vintages"] = "effective"
    as_of: Day | None = None

    @model_validator(mode="after")
    def cutoff(self) -> ObservationFilters:
        if self.mode == "vintages" and self.as_of is not None:
            raise ValueError("vintages cannot specify as_of")
        if self.mode == "effective" and self.as_of is not None and self.as_of < self.to_date:
            raise ValueError("as_of precedes to")
        return self


class ObservationKey(SeriesKey):
    observed_at: Day
    vintage_at: Instant


class ProviderFilters(DateRange):
    series_id: Identifier | None = None


class ProviderKey(InputModel):
    run_id: Identifier


class ReadingSelector(InputModel):
    as_of: Day
    series_id: Identifier | None = None


class ReadingIdentity(ReadingSelector):
    rules_revision: Identifier


class RunKey(InputModel):
    run_revision_id: Identifier


class RunSelector(OneSelector):
    run_revision_id: Identifier | None = None
    as_of_date: Day | None = None
    latest: Literal[True] | None = None


class AnalysisFilters(RunKey):
    ticker: Ticker | None = None


class AnalysisKey(RunKey):
    ticker: Ticker


class ReviewFilters(DateRange):
    run_revision_id: Identifier | None = None


class ReviewKey(InputModel):
    review_set_id: Identifier


class ReviewSelector(OneSelector):
    review_set_id: Identifier | None = None
    public_run_id: Identifier | None = None
    as_of: Day | None = None
    not_before: Day | None = None
    latest: Literal[True] | None = None


class CohortFilters(DateRange):
    snapshot_token: Identifier | None = None


class CohortKey(InputModel):
    asof: Day


class CohortSelector(CohortKey):
    snapshot_token: Identifier | None = None


class PanelFilters(CohortSelector):
    ticker: Ticker | None = None


class PanelKey(CohortKey):
    ticker: Ticker


class PanelSelector(PanelKey):
    snapshot_token: Identifier | None = None


class ForwardFilters(PanelFilters):
    horizon: Literal["3m", "6m", "1y", "3y", "5y"] | None = None


class ForwardKey(PanelKey):
    horizon: Literal["3m", "6m", "1y", "3y", "5y"]


class ForwardSelector(ForwardKey):
    snapshot_token: Identifier | None = None


class ContextKey(InputModel):
    context_id: Identifier


class ContextSelector(OneSelector):
    context_id: Identifier | None = None
    latest: Literal[True] | None = None
    as_of: Day | None = None


class TriageFilters(DateRange):
    review_set_id: Identifier | None = None


class TriageKey(InputModel):
    research_triage_id: Identifier


class TickerFilters(InputModel):
    ticker: Ticker | None = None


class TickerRange(DateRange):
    ticker: Ticker | None = None


class ThesisKey(InputModel):
    thesis_id: Identifier


class ThesisReviewFilters(InputModel):
    thesis_id: Identifier | None = None


class ThesisReviewKey(InputModel):
    review_id: Identifier


class AssessmentKey(InputModel):
    capital_allocation_assessment_id: Identifier


class PositionReviewKey(InputModel):
    position_review_id: Identifier


class LedgerEventFilters(TickerRange):
    event_type: (
        Literal[
            "opening_balance",
            "contribution",
            "withdrawal",
            "reservation",
            "release",
            "execution",
            "income",
            "cost",
            "tax_confirmed",
        ]
        | None
    ) = None


class LedgerEventKey(InputModel):
    event_id: Identifier


class PriceKey(InputModel):
    ticker: Ticker


class OutcomeFilters(DateRange):
    horizon: Literal["1y", "3y", "5y"] | None = None


class OutcomeKey(InputModel):
    outcome_id: Identifier


class TaskFilters(TickerFilters):
    status: Literal["open", "done", "dropped"] | None = None
    kind: Literal["earnings-review", "ops", "follow-up", "other"] | None = None


class TaskKey(InputModel):
    task_id: Identifier


class OperationFilters(TickerFilters):
    status: Literal["active", "completed"] | None = None
    session_kind: Literal["capital-allocation", "position-review"] | None = None


class OperationKey(InputModel):
    operation_id: Identifier


class CoverageFilters(InputModel):
    source: Identifier | None = None


class CoverageKey(InputModel):
    source: Identifier
    coverage_key: Identifier


class CapitalPolicyKey(InputModel):
    snapshot_month_end: Day
    ticker: Ticker
