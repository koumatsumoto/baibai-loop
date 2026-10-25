"""企業調査の依頼・対象選定・固定入力の契約。保存先や時計は読まない。"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import date
from hashlib import sha256
from typing import Annotated, Literal, Self

from pydantic import (
    AwareDatetime,
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    JsonValue,
    StringConstraints,
    model_validator,
)

from baibai_engine.data_collection.identifiers import normalize_ticker
from baibai_engine.foundation.json import canonical_json
from baibai_engine.foundation.time import JST
from baibai_engine.investment_judgment.selection_policy import ResearchCapacity, validate_capacity


def _ticker(value: object) -> str:
    if not isinstance(value, str):
        raise ValueError("ticker must be a string")
    return normalize_ticker(value)


type Ticker = Annotated[str, BeforeValidator(_ticker), Field(pattern=r"^[0-9A-Z]{4}$")]
type Text = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
type ResearchScope = Literal["discover_new", "specified_stocks", "review_positions"]


class _Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ResearchRequest(_Contract):
    text: Text
    scope: ResearchScope
    requested_tickers: tuple[Ticker, ...] = ()
    trigger: Literal["user_request"] = "user_request"

    @model_validator(mode="after")
    def scope_tickers(self) -> Self:
        if len(set(self.requested_tickers)) != len(self.requested_tickers):
            raise ValueError("requested tickers must be unique")
        if self.scope == "discover_new" and self.requested_tickers:
            raise ValueError("discover_new does not accept requested tickers")
        if self.scope == "specified_stocks" and not self.requested_tickers:
            raise ValueError("specified_stocks requires requested tickers")
        return self


class ResearchTarget(_Contract):
    ticker: Ticker
    purpose: Literal["new", "holding"]


class SelectedTarget(_Contract):
    ticker: Ticker
    purpose: Literal["new", "holding"]
    rationale: Text
    research_question: Text
    key_risk: Text


class InputGap(_Contract):
    ticker: Ticker
    reason: Text
    attempted_sources: tuple[Text, ...] = Field(min_length=1)


class ResearchSelectionDraft(_Contract):
    schema_version: Literal[1]
    request: ResearchRequest
    research_as_of: date
    input_known_at: AwareDatetime
    run_revision_id: Text | None = None
    selection_summary: Text
    targets: tuple[SelectedTarget, ...] = ()
    input_gaps: tuple[InputGap, ...] = ()
    company_growth_start: date | None = None
    company_growth_end: date | None = None

    @model_validator(mode="after")
    def validate_selection(self) -> Self:
        _validate_subjects(self.request, self.targets, self.input_gaps)
        end = self.company_growth_end or self.research_as_of
        if end > self.research_as_of or (
            self.company_growth_start is not None and self.company_growth_start > end
        ):
            raise ValueError("company growth requires start <= end <= research_as_of")
        if self.research_as_of > self.input_known_at.astimezone(JST).date():
            raise ValueError("research_as_of is after the input cutoff's JST date")
        return self


class AnalysisInputSnapshot(_Contract):
    schema_version: Literal[1] = 1
    run_revision_id: Text | None
    analysis_as_of: date | None
    run_at: AwareDatetime | None
    screening_rules_hash: Text | None
    er_model_version: Text | None
    analyses: dict[Ticker, dict[str, JsonValue]]
    unavailable: dict[Ticker, Text] = Field(default_factory=dict)

    @model_validator(mode="after")
    def identity(self) -> Self:
        if any(row.get("ticker") != ticker for ticker, row in self.analyses.items()):
            raise ValueError("analysis ticker differs from its key")
        if self.analyses.keys() & self.unavailable.keys():
            raise ValueError("analysis and unavailable tickers must be disjoint")
        if self.run_revision_id is None:
            if self.analyses or any(
                value is not None
                for value in (
                    self.analysis_as_of,
                    self.run_at,
                    self.screening_rules_hash,
                    self.er_model_version,
                )
            ):
                raise ValueError("a missing run cannot have analysis or run metadata")
        elif self.analysis_as_of is None or self.run_at is None:
            raise ValueError("a saved run requires analysis_as_of and run_at")
        return self


class ResearchInputsSnapshot(_Contract):
    analysis: AnalysisInputSnapshot
    market_context: dict[str, JsonValue]
    securities_context: dict[Ticker, dict[str, JsonValue]]
    input_gaps: tuple[InputGap, ...] = ()

    @model_validator(mode="after")
    def contexts(self) -> Self:
        required = {"market_context", "external_assessment"}
        allowed = required | {"technical_analysis", "company_growth"}
        if any(not required <= row.keys() <= allowed for row in self.securities_context.values()):
            raise ValueError(
                "security context requires market_context/external_assessment and optional "
                "technical_analysis/company_growth for retained snapshots"
            )
        return self


class ResearchSelection(_Contract):
    kind: Literal["research_selection"] = "research_selection"
    schema_version: Literal[2] = 2
    request: ResearchRequest
    research_as_of: date
    input_known_at: AwareDatetime
    selection_summary: Text
    targets: tuple[SelectedTarget, ...]
    capacity: ResearchCapacity
    inputs: ResearchInputsSnapshot

    @model_validator(mode="after")
    def fixed_inputs(self) -> Self:
        _validate_subjects(self.request, self.targets, self.inputs.input_gaps)
        validate_capacity(len(self.targets), self.capacity)
        tickers = {target.ticker for target in self.targets}
        analysis = self.inputs.analysis
        if set(analysis.analyses) | set(analysis.unavailable) != tickers:
            raise ValueError("analysis snapshot must exactly cover selected targets")
        if set(self.inputs.securities_context) != tickers:
            raise ValueError("security context must exactly cover selected targets")
        if analysis.unavailable != {gap.ticker: gap.reason for gap in self.inputs.input_gaps}:
            raise ValueError("input gaps must exactly explain unavailable analyses")
        if self.research_as_of > self.input_known_at.astimezone(JST).date():
            raise ValueError("research_as_of is after the input cutoff's JST date")
        if analysis.analysis_as_of is not None and analysis.analysis_as_of > self.research_as_of:
            raise ValueError("analysis_as_of is after research_as_of")
        if analysis.run_at is not None and analysis.run_at > self.input_known_at:
            raise ValueError("run_at is after input_known_at")
        return self


def _validate_subjects(
    request: ResearchRequest, targets: tuple[SelectedTarget, ...], gaps: tuple[InputGap, ...]
) -> None:
    tickers = {target.ticker for target in targets}
    if len(tickers) != len(targets):
        raise ValueError("selected tickers must be unique")
    expected = "holding" if request.scope == "review_positions" else "new"
    if any(target.purpose != expected for target in targets):
        raise ValueError("selected purpose differs from request scope")
    if request.requested_tickers and not tickers <= set(request.requested_tickers):
        raise ValueError("selected tickers are outside the requested set")
    if len({gap.ticker for gap in gaps}) != len(gaps) or any(
        gap.ticker not in tickers for gap in gaps
    ):
        raise ValueError("input gaps must be unique selected tickers")


def parse_selection(payload: Mapping[str, object]) -> ResearchSelection:
    if payload.get("schema_version") != 2:
        raise ValueError("Research selection requires an explicit V2 cutover")
    return ResearchSelection.model_validate(payload)


def selection_targets(selection: ResearchSelection) -> tuple[ResearchTarget, ...]:
    return tuple(
        ResearchTarget(ticker=item.ticker, purpose=item.purpose) for item in selection.targets
    )


def selection_sha256(selection: ResearchSelection) -> str:
    return sha256(canonical_json(selection.model_dump(mode="json")).encode()).hexdigest()
