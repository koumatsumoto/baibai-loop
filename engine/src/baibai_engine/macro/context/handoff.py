"""Validate external macro drafts before transcription can change a current judgment.

This noncanonical input contract serves the macro writing step: it stops broken
citations, unsupported numeric payloads and reuse of explicitly excluded evidence.
It neither establishes factual truth nor reads/writes a store or publishes a report.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Annotated, Literal, Protocol, Self

from pydantic import (
    AwareDatetime,
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
    model_validator,
)

from baibai_engine.foundation.time import JST
from baibai_engine.foundation.yaml_io import strict_safe_load

from .models import (
    CORE_SECTION_ORDER,
    MAX_READING_LAG_DAYS,
    SCENARIO_PROBABILITY_STEPS,
    TRANSMISSION_CHANNEL_SECTION_IDS,
    EconomicConnection,
    FactSummary,
    MacroConnectionSection,
    MacroCoreSectionId,
    MacroSynthesis,
    MaterialDelta,
    MonitoringPoint,
    RiskEnvironmentAssessment,
    SectionJudgment,
)


def _date_input(value: object) -> object:
    if type(value) is date or (
        isinstance(value, str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}", value)
    ):
        return value
    raise ValueError("date must be YYYY-MM-DD, not a timestamp or number")


def _timestamp_input(value: object) -> object:
    if isinstance(value, datetime) or (isinstance(value, str) and "T" in value):
        return value
    raise ValueError("timestamp must be an ISO datetime with timezone")


DateOnly = Annotated[date, BeforeValidator(_date_input)]
Timestamp = Annotated[AwareDatetime, BeforeValidator(_timestamp_input)]
Text = Annotated[str, StringConstraints(min_length=1, pattern=r"\S")]
Identifier = Annotated[str, StringConstraints(pattern=r"^[a-z0-9][a-z0-9._-]*$")]
Digest = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]
Number = Annotated[float, Field(strict=True, allow_inf_nan=False)]
References = Annotated[tuple[Identifier, ...], Field(min_length=1)]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ReadingBinding(_Strict):
    as_of: DateOnly
    rules_revision: Text
    payload_sha256: Digest | None = None


class MarketBinding(_Strict):
    release_id: Text
    manifest_sha256: Digest
    observation_as_of: DateOnly


class InputBindings(_Strict):
    reading: ReadingBinding | None
    market: MarketBinding | None
    limitations: tuple[Text, ...] = ()

    @model_validator(mode="after")
    def explain_missing_binding(self) -> Self:
        if (self.reading is None or self.market is None) and not self.limitations:
            raise ValueError("missing input binding requires limitations")
        return self


class ObservedValue(_Strict):
    observed_at: DateOnly
    value: Number
    vintage_at: Timestamp | None = None


class NumericResult(_Strict):
    label: Text
    value: Number
    unit: Text


class ExternalSource(_Strict):
    source_id: Identifier
    publisher: Text
    title: Text
    url: Annotated[str, StringConstraints(pattern=r"^https?://\S+$")]
    published_on: DateOnly | None
    accessed_at: Timestamp
    status: Literal["ok", "failed", "unverified"]
    limitation: Text | None = None

    @model_validator(mode="after")
    def explain_source_status(self) -> Self:
        if self.status == "ok" and self.published_on is None:
            raise ValueError("ok source requires a known publication date")
        if self.status != "ok" and self.limitation is None:
            raise ValueError("failed/unverified source requires limitation")
        return self


class _Evidence(_Strict):
    evidence_id: Identifier
    summary: Text


class SeriesEvidence(_Evidence):
    kind: Literal["series"]
    series_id: Text
    unit: Text
    source_url: Annotated[str, StringConstraints(pattern=r"^https?://\S+$")]
    accessed_at: Timestamp
    observations: tuple[ObservedValue, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def ordered_observations(self) -> Self:
        dates = [point.observed_at for point in self.observations]
        if dates != sorted(set(dates)):
            raise ValueError("observations must have unique ascending dates")
        return self


class ArticleEvidence(_Evidence):
    kind: Literal["article"]
    external_source_id: Identifier


class MarketEvidence(_Evidence):
    kind: Literal["market"]
    observation_as_of: DateOnly
    accessed_at: Timestamp
    method: Text
    results: tuple[NumericResult, ...] = Field(min_length=1)


class CalculationEvidence(_Evidence):
    kind: Literal["calculation"]
    input_evidence_ids: References
    observation_as_of: DateOnly
    method: Text
    results: tuple[NumericResult, ...] = Field(min_length=1)


Evidence = Annotated[
    SeriesEvidence | ArticleEvidence | MarketEvidence | CalculationEvidence,
    Field(discriminator="kind"),
]


class DataIssue(_Strict):
    issue_id: Identifier
    severity: Literal["exclude", "warning"]
    reason: Text
    affected_series: tuple[Text, ...] = ()
    evidence_ids: tuple[Identifier, ...] = ()
    allowed_substitutes: tuple[Text, ...] = ()

    @model_validator(mode="after")
    def require_target(self) -> Self:
        if not self.affected_series and not self.evidence_ids:
            raise ValueError("data issue requires a series or evidence target")
        return self


class HandoffCoreSection(_Strict):
    section_id: MacroCoreSectionId
    series_ids: tuple[Text, ...] = Field(min_length=1)
    fact_summary: tuple[FactSummary, ...] = Field(min_length=1)
    judgment: SectionJudgment
    economic_connection: EconomicConnection
    material_deltas: tuple[MaterialDelta, ...] = ()

    @model_validator(mode="after")
    def validate_section(self) -> Self:
        _unique(self.series_ids, "section series_ids")
        if self.material_deltas and self.section_id not in TRANSMISSION_CHANNEL_SECTION_IDS:
            raise ValueError("material deltas require a transmission-channel section")
        return self

    def statements(self) -> tuple[_Sourced, ...]:
        return (*self.fact_summary, self.judgment, self.economic_connection, *self.material_deltas)


class ScorecardIntent(_Strict):
    series_id: Text
    expectation: Text
    rationale: Text


class HandoffScenario(FactSummary):
    case: Literal["base", "bear", "bull"]
    direction: Literal["supportive", "adverse", "mixed"]
    probability: Annotated[
        float,
        Field(strict=True, allow_inf_nan=False, ge=0.05, le=0.90, multiple_of=0.05),
    ]
    conditions: tuple[Text, ...] = Field(min_length=1)
    economic_implications: tuple[Text, ...] = Field(min_length=1)
    scorecard_intents: tuple[ScorecardIntent, ...] = Field(min_length=2)

    @model_validator(mode="after")
    def distinct_intents(self) -> Self:
        keys = tuple((item.series_id, item.expectation) for item in self.scorecard_intents)
        if len(keys) != len(set(keys)):
            raise ValueError("scorecard intents must differ")
        return self


class HandoffMonitoringPoint(MonitoringPoint):
    # The canonical reader ignores a retired field on immutable old publications.
    # New handoffs must not silently discard it or any other unknown field.
    model_config = ConfigDict(extra="forbid", frozen=True)


class _Sourced(Protocol):
    @property
    def source_ids(self) -> tuple[str, ...]: ...


@dataclass(frozen=True)
class _ResolvedEvidence:
    series: frozenset[str]
    market: bool
    usable: bool
    observation_as_of: date


def _unique(values: tuple[str, ...], label: str) -> None:
    if len(values) != len(set(values)):
        raise ValueError(f"duplicate {label}")


class MacroContextHandoff(_Strict):
    schema_version: Literal[1]
    kind: Literal["macro-context-handoff"]
    analysis_id: Annotated[
        str, StringConstraints(pattern=r"^macro-handoff-\d{4}-\d{2}-\d{2}-[a-z0-9-]+$")
    ]
    as_of: DateOnly
    generated_at: Timestamp
    summary: Text
    bindings: InputBindings
    external_sources: tuple[ExternalSource, ...]
    evidence: tuple[Evidence, ...] = Field(min_length=1)
    core: tuple[HandoffCoreSection, ...] = Field(min_length=10, max_length=10)
    synthesis: MacroSynthesis
    risk_environment: RiskEnvironmentAssessment
    scenarios: tuple[HandoffScenario, ...] = Field(min_length=3, max_length=3)
    monitoring: tuple[HandoffMonitoringPoint, ...] = Field(min_length=1)
    connection: MacroConnectionSection
    data_issues: tuple[DataIssue, ...] = ()
    limitations: tuple[Text, ...] = ()

    @field_validator("schema_version", mode="before")
    @classmethod
    def literal_integer_version(cls, value: object) -> object:
        if type(value) is not int:
            raise ValueError("schema_version must be an integer")
        return value

    @model_validator(mode="after")
    def validate_contract(self) -> Self:
        if not self.analysis_id.startswith(f"macro-handoff-{self.as_of.isoformat()}-"):
            raise ValueError("analysis_id date must equal as_of")
        if self.generated_at.astimezone(JST).date() < self.as_of:
            raise ValueError("generated_at must not predate as_of in JST")
        if tuple(section.section_id for section in self.core) != CORE_SECTION_ORDER:
            raise ValueError("core requires the canonical ten sections in order")
        if not any(section.material_deltas for section in self.core):
            raise ValueError("at least one material delta is required")
        if tuple(scenario.case for scenario in self.scenarios) != ("base", "bear", "bull"):
            raise ValueError("scenarios must be base, bear, bull in order")
        if (
            sum(round(s.probability * SCENARIO_PROBABILITY_STEPS) for s in self.scenarios)
            != SCENARIO_PROBABILITY_STEPS
        ):
            raise ValueError("scenario probabilities must sum to 1.0")
        self._validate_dates()
        resolved = self._resolve_evidence()
        self._validate_claims(resolved)
        return self

    def _validate_dates(self) -> None:
        def observation(day: date) -> None:
            if day > self.as_of:
                raise ValueError("evidence observes beyond as_of")

        def accessed(instant: datetime) -> None:
            if instant > self.generated_at:
                raise ValueError("access/vintage time exceeds generated_at")

        reading = self.bindings.reading
        if (
            reading is not None
            and not 0 <= (self.as_of - reading.as_of).days <= MAX_READING_LAG_DAYS
        ):
            raise ValueError("reading binding lies outside the canonical reading window")
        if self.bindings.market is not None:
            observation(self.bindings.market.observation_as_of)
        for source in self.external_sources:
            if source.published_on is not None:
                observation(source.published_on)
                if source.published_on > source.accessed_at.astimezone(JST).date():
                    raise ValueError("source publication date exceeds access date")
            accessed(source.accessed_at)
        for evidence in self.evidence:
            if isinstance(evidence, SeriesEvidence):
                accessed(evidence.accessed_at)
                for point in evidence.observations:
                    observation(point.observed_at)
                    if point.observed_at > evidence.accessed_at.astimezone(JST).date():
                        raise ValueError("series observation date exceeds access date")
                    if point.vintage_at is not None:
                        accessed(point.vintage_at)
                        if point.vintage_at > evidence.accessed_at:
                            raise ValueError("observation vintage exceeds evidence access time")
            elif isinstance(evidence, (MarketEvidence, CalculationEvidence)):
                observation(evidence.observation_as_of)
                if isinstance(evidence, MarketEvidence):
                    accessed(evidence.accessed_at)
                    if evidence.observation_as_of > evidence.accessed_at.astimezone(JST).date():
                        raise ValueError("market observation date exceeds access date")
                    binding = self.bindings.market
                    if (
                        binding is not None
                        and evidence.observation_as_of > binding.observation_as_of
                    ):
                        raise ValueError("market evidence exceeds its binding observation date")

    def _resolve_evidence(self) -> dict[str, _ResolvedEvidence]:
        _unique(tuple(item.evidence_id for item in self.evidence), "evidence IDs")
        _unique(tuple(item.source_id for item in self.external_sources), "external source IDs")
        _unique(tuple(item.issue_id for item in self.data_issues), "data issue IDs")
        evidence = {item.evidence_id: item for item in self.evidence}
        sources = {item.source_id: item for item in self.external_sources}
        excluded_ids: set[str] = set()
        excluded_series: set[str] = set()
        for issue in self.data_issues:
            if set(issue.evidence_ids) - evidence.keys():
                raise ValueError("data issue references unknown evidence")
            if issue.severity == "exclude":
                excluded_ids.update(issue.evidence_ids)
                excluded_series.update(issue.affected_series)
        resolved: dict[str, _ResolvedEvidence] = {}
        visiting: set[str] = set()

        def resolve(evidence_id: str) -> _ResolvedEvidence:
            if evidence_id in resolved:
                return resolved[evidence_id]
            if evidence_id not in evidence:
                raise ValueError("unknown evidence reference")
            if evidence_id in visiting:
                raise ValueError("cyclic calculation evidence")
            visiting.add(evidence_id)
            item = evidence[evidence_id]
            series: frozenset[str] = frozenset()
            market = False
            usable = True
            latest = self.as_of
            if isinstance(item, SeriesEvidence):
                series = frozenset((item.series_id,))
                latest = item.observations[-1].observed_at
            elif isinstance(item, ArticleEvidence):
                if item.external_source_id not in sources:
                    raise ValueError("unknown external source reference")
                usable = sources[item.external_source_id].status == "ok"
                latest = sources[item.external_source_id].published_on or date.min
            elif isinstance(item, MarketEvidence):
                market = True
                latest = item.observation_as_of
            else:
                _unique(item.input_evidence_ids, "calculation input IDs")
                parents = [resolve(parent) for parent in item.input_evidence_ids]
                series = frozenset().union(*(parent.series for parent in parents))
                market = any(parent.market for parent in parents)
                usable = all(parent.usable for parent in parents)
                latest = item.observation_as_of
                if latest < max(parent.observation_as_of for parent in parents):
                    raise ValueError("calculation date predates its input evidence")
            usable = (
                usable and evidence_id not in excluded_ids and not bool(series & excluded_series)
            )
            visiting.remove(evidence_id)
            result = _ResolvedEvidence(series, market, usable, latest)
            resolved[evidence_id] = result
            return result

        for evidence_id in evidence:
            resolve(evidence_id)
        return resolved

    def _validate_claims(self, resolved: dict[str, _ResolvedEvidence]) -> None:
        def cited(items: tuple[_Sourced, ...]) -> frozenset[str]:
            series: set[str] = set()
            for item in items:
                for evidence_id in item.source_ids:
                    if evidence_id not in resolved:
                        raise ValueError("statement references unknown evidence")
                    entry = resolved[evidence_id]
                    if not entry.usable:
                        raise ValueError("statement cites excluded/failed/unverified evidence")
                    series.update(entry.series)
            return frozenset(series)

        by_section = {section.section_id: section for section in self.core}
        for section in self.core:
            if set(section.series_ids) - cited(section.statements()):
                raise ValueError("section series lack cited evidence")
        cited((self.risk_environment, *self.scenarios, *self.monitoring))
        risk_series = set(by_section["risk_environment"].series_ids)
        for scenario in self.scenarios:
            intent_series = {intent.series_id for intent in scenario.scorecard_intents}
            if intent_series - risk_series or intent_series - cited((scenario,)):
                raise ValueError("scorecard intents need risk-section and scenario evidence")
        for force in self.synthesis.dominant_forces:
            if len(force.core_section_ids) < 2 or len(force.series_ids) < 2:
                raise ValueError("dominant force requires two channels and two series")
            contributions = [set(by_section[key].series_ids) for key in force.core_section_ids]
            named = set().union(*contributions)
            if set(force.series_ids) - named or set(force.series_ids) - cited((force,)):
                raise ValueError("dominant force lacks evidence in its named sections")
            if any(not group.intersection(force.series_ids) for group in contributions):
                raise ValueError("dominant force names an unsupported channel")
        cited(self.synthesis.interactions)
        connection = self.connection
        if connection.bargain_topography is None or not connection.estimate_caveats:
            raise ValueError("connection requires bargain topography and estimate caveats")
        available = {
            series for key in connection.core_section_ids for series in by_section[key].series_ids
        }
        connection_sources = cited(
            (
                *connection.fact_summary,
                connection.judgment,
                *connection.research_priority_hints,
                *connection.sector_tilts,
                *connection.sizing_cautions,
                connection.bargain_topography,
                *connection.estimate_caveats,
            )
        )
        if (
            set(connection.series_ids) - available
            or set(connection.series_ids) - connection_sources
        ):
            raise ValueError("connection series must be supported by named core sections")
        if not any(resolved[key].market for key in connection.bargain_topography.source_ids):
            raise ValueError("bargain topography requires market evidence")


def _unique_json_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON object key")
        result[key] = value
    return result


def _reject_json_constant(value: str) -> None:
    raise ValueError("non-finite JSON number")


def load_handoff(path: Path) -> MacroContextHandoff:
    """Load one explicit local file; never follow URLs or execute method strings."""
    text = path.read_text(encoding="utf-8")
    if path.suffix.lower() == ".json":
        raw = json.loads(
            text, object_pairs_hook=_unique_json_object, parse_constant=_reject_json_constant
        )
    elif path.suffix.lower() in {".yaml", ".yml"}:
        raw = strict_safe_load(text)
    else:
        raise ValueError("handoff file must have .yaml, .yml or .json suffix")
    return MacroContextHandoff.model_validate(raw)
