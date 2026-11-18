"""企業調査の依頼・対象選定・固定入力の契約。保存先や時計は読まない。"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime
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

from baibai_engine.investment_judgment.company_research.context import (
    AvailableAssessment,
    AvailableResearch,
    CompanyContext,
)
from baibai_engine.investment_judgment.workflow.selection_policy import (
    CURRENT_RESEARCH_CAPACITY,
    ResearchCapacity,
    validate_capacity,
)
from baibai_engine.machine_analysis.generation.contracts import ModelInputSlice, research_subjects
from baibai_engine.machine_analysis.screening.contracts import (
    ScreeningSelection,
)
from baibai_engine.primitives.json import encode_json
from baibai_engine.primitives.stocks import normalize_ticker
from baibai_engine.primitives.time import JST


def _ticker(value: object) -> str:
    if not isinstance(value, str):
        raise ValueError("ticker must be a string")
    return normalize_ticker(value)


type Ticker = Annotated[str, BeforeValidator(_ticker), Field(pattern=r"^[0-9A-Z]{4}$")]
type Text = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
type ResearchScope = Literal["discover_new", "specified_stocks", "review_positions"]


def validate_input_known_at(known_at: datetime, *, now: datetime) -> None:
    if known_at.utcoffset() is None:
        raise ValueError("input cutoff must have a timezone")
    if now.utcoffset() is None:
        raise ValueError("execution time must have a timezone")
    if known_at > now:
        raise ValueError("input_known_at is after execution time")


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


class GeneratedResearchInputs(_Contract):
    """生成済みモデルの対象別コピー。旧runや可変storeを再計算しない。"""

    generation: ModelInputSlice
    screening_selection_id: Text | None = None
    screening_selection: ScreeningSelection | None = None
    screening_published_at: AwareDatetime | None = None
    saved_market_context_assessment: dict[str, JsonValue] | None = None

    @model_validator(mode="after")
    def fixed_policy(self) -> Self:
        present = (
            self.screening_selection_id is not None,
            self.screening_selection is not None,
            self.screening_published_at is not None,
        )
        if any(present) and not all(present):
            raise ValueError("screening identity, result and publication time must be paired")
        if (
            self.screening_selection is not None
            and self.screening_selection.generation_id != self.generation.generation_id
        ):
            raise ValueError("screening selection differs from the chosen generation")
        return self


class GeneratedResearchSelectionDraft(_Contract):
    schema_version: Literal[2]
    request: ResearchRequest
    research_as_of: date
    input_known_at: AwareDatetime
    generation_id: Text
    screening_selection_id: Text | None = None
    selection_summary: Text
    targets: tuple[SelectedTarget, ...] = ()
    comparison_tickers: tuple[Ticker, ...] = Field(default=(), exclude_if=lambda value: not value)

    @model_validator(mode="after")
    def validate_selection(self) -> Self:
        _validate_comparisons(
            self.comparison_tickers,
            self.targets,
            self.screening_selection_id,
            CURRENT_RESEARCH_CAPACITY,
        )
        _validate_subjects(self.request, self.targets, ())
        if self.research_as_of > self.input_known_at.astimezone(JST).date():
            raise ValueError("research_as_of is after the input cutoff's JST date")
        return self


class GeneratedSelection(_Contract):
    kind: Literal["research_selection"] = "research_selection"
    schema_version: int
    request: ResearchRequest
    research_as_of: date
    input_known_at: AwareDatetime
    selection_summary: Text
    targets: tuple[SelectedTarget, ...]
    capacity: ResearchCapacity
    inputs: GeneratedResearchInputs

    @model_validator(mode="after")
    def fixed_inputs(self) -> Self:
        _validate_subjects(self.request, self.targets, ())
        validate_capacity(len(self.targets), self.capacity)
        generation = self.inputs.generation
        if self.request.scope == "discover_new" and self.inputs.screening_selection_id is None:
            raise ValueError("discovery requires an explicit saved screening selection")
        if set(generation.subjects) != set(research_subjects(generation, self.evidence_subjects())):
            raise ValueError(
                "model input scope must exactly cover targets and their shared contexts"
            )
        if self.research_as_of > self.input_known_at.astimezone(JST).date():
            raise ValueError("research_as_of is after the input cutoff's JST date")
        if generation.as_of > self.research_as_of:
            raise ValueError("generation as_of is after research_as_of")
        if generation.evaluation_time > self.input_known_at:
            raise ValueError("generation evaluation_time is after input_known_at")
        if (
            self.inputs.screening_published_at is not None
            and self.inputs.screening_published_at > self.input_known_at
        ):
            raise ValueError("screening publication is after input_known_at")
        return self

    def evidence_subjects(self) -> tuple[str, ...]:
        return tuple(t.ticker for t in self.targets)


class GeneratedResearchSelection(GeneratedSelection):
    """旧V3のfield集合・dumpを保持する。"""

    schema_version: Literal[3] = 3


class GeneratedResearchInputsV4(GeneratedResearchInputs):
    saved_company_context: dict[Ticker, CompanyContext]


class GeneratedSelectionWithContext(GeneratedSelection):
    """prepare時点の開始材料。最終Assessmentの全因果provenanceではない。"""

    inputs: GeneratedResearchInputsV4

    @model_validator(mode="after")
    def fixed_company_context(self) -> Self:
        if set(self.inputs.saved_company_context) != {target.ticker for target in self.targets}:
            raise ValueError("saved company context must exactly cover selected targets")
        for ticker, context in self.inputs.saved_company_context.items():
            if isinstance(context.research, AvailableResearch):
                record = context.research.record
                if (
                    record.document.ticker != ticker
                    or record.document.as_of > self.research_as_of
                    or record.recorded_at > self.input_known_at
                ):
                    raise ValueError("saved Research differs from ticker or cutoff")
            if isinstance(context.assessment, AvailableAssessment):
                assessment = context.assessment
                if (
                    assessment.ticker != ticker
                    or assessment.as_of > self.research_as_of
                    or assessment.published_at > self.input_known_at
                ):
                    raise ValueError("saved Assessment differs from ticker or cutoff")
        return self


class GeneratedResearchSelectionV4(GeneratedSelectionWithContext):
    schema_version: Literal[4] = 4


@dataclass(frozen=True, slots=True)
class PopulationGroup:
    """分類だけ。全社を実際に調査したとも、欠測を落選とも扱わない。"""

    scope: str
    technical_state: str | None
    financial_state: str | None
    tickers: tuple[str, ...]
    models_recorded: Annotated[bool | None, Field(exclude_if=lambda v: v is None)] = None


@dataclass(frozen=True, slots=True)
class CaseScreening(ScreeningSelection):
    """resultの明細は有限行のみ。全体の明細はsource_sha256が指す発行物に属する。"""

    source_sha256: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")] = field(kw_only=True)
    population_groups: tuple[PopulationGroup, ...] = field(kw_only=True)
    legacy_candidate_count: Annotated[int | None, Field(ge=0, exclude_if=lambda v: v is None)] = (
        field(kw_only=True, default=None)
    )


class GeneratedResearchInputsV5(GeneratedResearchInputsV4):
    screening_selection: CaseScreening | None = None


class GeneratedResearchSelectionV5(GeneratedSelectionWithContext):
    """母集団の識別と有限Caseの証拠。対照は深掘りや提案対象ではない。"""

    schema_version: Literal[5] = 5
    comparison_tickers: tuple[Ticker, ...] = Field(default=(), exclude_if=lambda value: not value)
    inputs: GeneratedResearchInputsV5

    def evidence_subjects(self) -> tuple[str, ...]:
        return (*super().evidence_subjects(), *self.comparison_tickers)

    @model_validator(mode="after")
    def explicit_comparisons(self) -> Self:
        _validate_comparisons(
            self.comparison_tickers, self.targets, self.inputs.screening_selection_id, self.capacity
        )
        return self


def _validate_comparisons(
    comparisons: tuple[str, ...],
    targets: tuple[SelectedTarget, ...],
    selection_id: str | None,
    capacity: ResearchCapacity,
) -> None:
    # A separate evidence budget: do not recreate the full market by naming every company.
    if len(comparisons) > capacity.max_deep_research_targets:
        raise ValueError("explicit comparison evidence exceeds this Case's research capacity")
    if (
        (comparisons and selection_id is None)
        or len(set(comparisons)) != len(comparisons)
        or set(comparisons).intersection(t.ticker for t in targets)
    ):
        raise ValueError(
            "explicit screening comparisons require a publication and distinct non-target tickers"
        )


type ResearchSelectionRecord = (
    ResearchSelection
    | GeneratedResearchSelection
    | GeneratedResearchSelectionV4
    | GeneratedResearchSelectionV5
)


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


def parse_selection(payload: Mapping[str, object]) -> ResearchSelectionRecord:
    if payload.get("schema_version") == 5:
        return GeneratedResearchSelectionV5.model_validate(payload)
    if payload.get("schema_version") == 4:
        return GeneratedResearchSelectionV4.model_validate(payload)
    if payload.get("schema_version") == 3:
        return GeneratedResearchSelection.model_validate(payload)
    if payload.get("schema_version") != 2:
        raise ValueError("Research selection requires an explicit V2 cutover")
    return ResearchSelection.model_validate(payload)


def selection_from_artifacts(
    artifacts: Sequence[Mapping[str, object]],
) -> ResearchSelectionRecord | None:
    rows = [item for item in artifacts if item.get("kind") == "research_selection"]
    if not rows:
        return None
    if len(rows) != 1:
        raise ValueError("one fixed research_selection is required")
    return parse_selection(rows[0])


def selection_targets(selection: ResearchSelectionRecord) -> tuple[ResearchTarget, ...]:
    return tuple(
        ResearchTarget(ticker=item.ticker, purpose=item.purpose) for item in selection.targets
    )


def selection_payload_and_sha256(
    selection: ResearchSelectionRecord,
) -> tuple[dict[str, object], str]:
    """Return the same normalized document whose bytes produce the selection identity."""
    payload = selection.model_dump(mode="json")
    return payload, sha256(encode_json(payload).encode()).hexdigest()


def selection_sha256(selection: ResearchSelectionRecord) -> str:
    return selection_payload_and_sha256(selection)[1]


def saved_market_assessment(selection: ResearchSelectionRecord) -> JsonValue:
    if isinstance(selection, GeneratedSelection):
        return selection.inputs.saved_market_context_assessment
    return selection.inputs.market_context.get("saved_market_context_assessment")
