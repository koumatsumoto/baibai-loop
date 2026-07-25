"""Canonical macro-context contract owned by the engine.

The report is two parts. ``core`` is a use-case agnostic assessment of the market
environment: it reads on its own and carries no vocabulary of the Japanese equity
accumulation loop. ``connection`` is the only place that translates the assessment
into that loop (research priority, sector tilt, sizing caution).

The separation is enforced by reference direction rather than by the author's care:
connection may only cite series that core already cites, and names the core sections it
builds on. The loop-specific *fields* (sector tilt, research priority, sizing caution)
exist only on the connection section, so they cannot be placed in core at all. Prose is
not policed — a judgment written in core can still smuggle in an instruction, which is
what the skill's adversarial self-check is for.
"""

from __future__ import annotations

import re
from calendar import monthrange
from collections.abc import Mapping
from datetime import date, datetime
from functools import lru_cache
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, HttpUrl, field_validator, model_validator

from baibai_engine.macro.indicators.definitions import load_definitions

type MacroCoreSectionId = Literal[
    "regime_summary",
    "rates_policy",
    "growth_demand",
    "inflation_costs",
    "liquidity_credit",
    "fx",
    "japan",
    "valuation",
    "risk_environment",
    "monitoring",
]

CORE_SECTION_ORDER: tuple[MacroCoreSectionId, ...] = (
    "regime_summary",
    "rates_policy",
    "growth_demand",
    "inflation_costs",
    "liquidity_credit",
    "fx",
    "japan",
    "valuation",
    "risk_environment",
    "monitoring",
)

# The transmission-channel sections. A material delta is a change in an economic
# path, so it belongs where that path is examined — not in the summary that opens
# the report, the scenario section, or the monitoring list.
MATERIAL_DELTA_SECTION_IDS: frozenset[MacroCoreSectionId] = frozenset(
    {
        "rates_policy",
        "growth_demand",
        "inflation_costs",
        "liquidity_credit",
        "fx",
        "japan",
        "valuation",
    }
)

CONNECTION_SECTION_ID = "japan_equity_loop"

# The contract version that consumers read. Revisions stored under an earlier version
# stay in the table as a log and are filtered out of every read path.
MACRO_CONTEXT_SCHEMA_VERSION = 4

# A scorecard condition exists to be settled by a later report. A deadline beyond this
# horizon cannot be settled while the scenario is still the operative one, which would
# leave the scenario unfalsifiable in practice; the bound is wide enough for a quarterly
# series to print twice.
SCORECARD_HORIZON_MONTHS = 18

# A scorecard condition also needs room for at least one further print of its series to
# land and be observed; a deadline before that cannot be settled either.
MIN_SCORECARD_DAYS_BY_FREQUENCY: Mapping[str, int] = {
    "daily": 14,
    "weekly": 30,
    "monthly": 60,
    "quarterly": 150,
}

# The reading is recomputable for any as-of, so a report cites the reading of its own
# as-of. This allowance covers writing across a weekend, not reading an old snapshot.
MAX_READING_LAG_DAYS = 7


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class _SourcedStatement(_StrictModel):
    summary: str = Field(min_length=1)
    source_ids: tuple[str, ...] = Field(min_length=1)

    @field_validator("summary")
    @classmethod
    def require_non_blank_summary(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("summary must be non-blank")
        return value

    @field_validator("source_ids")
    @classmethod
    def require_unique_non_blank_sources(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if any(not value.strip() for value in values):
            raise ValueError("source_ids must be non-blank")
        if len(values) != len(set(values)):
            raise ValueError("source_ids must be unique")
        return values


class _TimestampedInput(_StrictModel):
    @field_validator("published_at", "accessed_at", check_fields=False)
    @classmethod
    def require_timezone(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("macro input datetime must include a timezone")
        return value


class ArticleInput(_TimestampedInput):
    input_id: str = Field(min_length=1)
    source: str = Field(min_length=1)
    title: str = Field(min_length=1)
    url: HttpUrl
    published_at: datetime
    accessed_at: datetime
    status: Literal["ok", "failed"]
    used_for: str = Field(min_length=1)


class IndicatorSeriesInput(_TimestampedInput):
    input_id: str = Field(min_length=1)
    provider: str = Field(min_length=1)
    series_id: str = Field(min_length=1)
    window: str = Field(min_length=1)
    observation_as_of: date
    published_at: datetime
    accessed_at: datetime
    status: Literal["ok", "failed"]
    used_for: str = Field(min_length=1)


class ReadingSnapshotInput(_TimestampedInput):
    """A cited macro reading snapshot, identified by its rules revision and as-of.

    The snapshot has no store of its own: ``macro reading`` recomputes it from the L1
    store for any past as-of, so the revision plus the date is its full identity.
    """

    input_id: str = Field(min_length=1)
    rules_revision: str = Field(min_length=1)
    reading_asof: date
    accessed_at: datetime
    status: Literal["ok", "failed"]
    used_for: str = Field(min_length=1)


class MachineSnapshotInput(_TimestampedInput):
    """A cited output of this repository's own deterministic commands.

    An internal output has no publisher and no URL: the command plus the date it was
    asked about is its identity, the way a reading snapshot's is its revision plus
    as-of. Filing one as an article would name a documentation page as the source of
    numbers the repository computed itself.
    """

    input_id: str = Field(min_length=1)
    command: str = Field(min_length=1)
    snapshot_asof: date
    # The latest market date the snapshot actually used, which trails its as-of by the
    # market calendar (a Friday close answers a Sunday as-of).
    observation_as_of: date
    accessed_at: datetime
    status: Literal["ok", "failed"]
    used_for: str = Field(min_length=1)


class MacroInputs(_StrictModel):
    articles: tuple[ArticleInput, ...]
    indicator_series: tuple[IndicatorSeriesInput, ...]
    reading_snapshots: tuple[ReadingSnapshotInput, ...]
    machine_snapshots: tuple[MachineSnapshotInput, ...] = ()


class FactSummary(_SourcedStatement):
    pass


class SectionJudgment(_SourcedStatement):
    direction: Literal["supportive", "adverse", "mixed"]
    confidence: Literal["low", "medium", "high"]


class EconomicConnection(_SourcedStatement):
    """How the section's reading transmits into economic paths.

    Deliberately free of loop vocabulary: no sector tilt, no research priority, no
    sizing. Those live in the connection section.
    """


class MaterialDelta(_SourcedStatement):
    channel: Literal["discount_rate", "demand", "funding", "common_tail"]
    direction: Literal["supportive", "adverse", "mixed"]
    materiality: Literal["low", "medium", "high"]
    used_for: str = Field(min_length=1)

    @field_validator("used_for")
    @classmethod
    def require_non_blank_use(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("used_for must be non-blank")
        return value


class RiskEnvironmentAssessment(_SourcedStatement):
    """Whether the environment rewards taking risk, and what would disprove it."""

    stance: Literal["risk_seeking", "neutral", "risk_averse"]
    confidence: Literal["low", "medium", "high"]
    falsifiers: tuple[str, ...] = Field(min_length=1)

    @field_validator("falsifiers")
    @classmethod
    def require_non_blank_falsifiers(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if any(not value.strip() for value in values):
            raise ValueError("falsifiers must be non-blank")
        return values


class ScorecardCondition(_StrictModel):
    """A scenario condition that a later report can settle from the L1 history alone."""

    series_id: str = Field(min_length=1)
    comparison: Literal["below", "at_or_below", "above", "at_or_above"]
    threshold: float = Field(allow_inf_nan=False)
    deadline: date


class MacroScenario(_SourcedStatement):
    case: Literal["base", "bear", "bull"]
    direction: Literal["supportive", "adverse", "mixed"]
    conditions: tuple[str, ...] = Field(min_length=1)
    # Two machine-checkable conditions per scenario: a single observation can be met
    # by accident, and a scenario that cannot state two of them is not yet a scenario.
    scorecard: tuple[ScorecardCondition, ...] = Field(min_length=2)
    economic_implications: tuple[str, ...] = Field(min_length=1)

    @field_validator("conditions", "economic_implications")
    @classmethod
    def require_non_blank_items(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if any(not value.strip() for value in values):
            raise ValueError("scenario items must be non-blank")
        return values

    @field_validator("scorecard")
    @classmethod
    def require_distinct_conditions(
        cls, values: tuple[ScorecardCondition, ...]
    ) -> tuple[ScorecardCondition, ...]:
        # Otherwise the two-condition rule is satisfied by writing one twice, which is
        # exactly the single observation met by accident that the rule exists to prevent.
        keys = [(item.series_id, item.comparison, item.threshold) for item in values]
        if len(keys) != len(set(keys)):
            raise ValueError("scorecard conditions must differ from each other")
        return values


class MonitoringPoint(_SourcedStatement):
    event: str = Field(min_length=1)
    condition: str = Field(min_length=1)
    view_change: str = Field(min_length=1)

    @field_validator("event", "condition", "view_change")
    @classmethod
    def require_non_blank_fields(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("monitoring point fields must be non-blank")
        return value


class MacroCoreSection(_StrictModel):
    section_id: MacroCoreSectionId
    series_ids: tuple[str, ...] = Field(min_length=1)
    fact_summary: tuple[FactSummary, ...] = Field(min_length=1)
    judgment: SectionJudgment
    economic_connection: EconomicConnection
    change_since_previous: str | None = None
    previous_scorecard_review: str | None = None
    material_deltas: tuple[MaterialDelta, ...] = ()
    risk_environment: RiskEnvironmentAssessment | None = None
    scenarios: tuple[MacroScenario, ...] = ()
    monitoring_points: tuple[MonitoringPoint, ...] = ()

    @model_validator(mode="after")
    def validate_section_contract(self) -> Self:
        if len(self.series_ids) != len(set(self.series_ids)):
            raise ValueError("section series_ids must be unique")
        self._validate_regime_summary_fields()
        if self.material_deltas and self.section_id not in MATERIAL_DELTA_SECTION_IDS:
            raise ValueError("material deltas belong in the transmission-channel sections")
        self._validate_risk_environment_section()
        if self.section_id == "monitoring":
            if not self.monitoring_points:
                raise ValueError("monitoring section requires monitoring points")
        elif self.monitoring_points:
            raise ValueError("monitoring points belong in the monitoring section")
        return self

    def _validate_regime_summary_fields(self) -> None:
        if self.section_id == "regime_summary":
            for name, value in (
                ("change from the previous context", self.change_since_previous),
                ("review of the previous scorecard", self.previous_scorecard_review),
            ):
                if value is None or not value.strip():
                    raise ValueError(f"regime summary requires the {name}")
            return
        if self.change_since_previous is not None:
            raise ValueError("change from the previous context belongs in the regime summary")
        if self.previous_scorecard_review is not None:
            raise ValueError("the previous scorecard review belongs in the regime summary")

    def _validate_risk_environment_section(self) -> None:
        if self.section_id == "risk_environment":
            if self.risk_environment is None:
                raise ValueError("risk environment section requires the risk appetite assessment")
            if tuple(item.case for item in self.scenarios) != ("base", "bear", "bull"):
                raise ValueError("risk environment section must contain base, bear, bull in order")
            cited = set(self.series_ids)
            unknown = sorted(
                {
                    condition.series_id
                    for scenario in self.scenarios
                    for condition in scenario.scorecard
                    if condition.series_id not in cited
                }
            )
            if unknown:
                raise ValueError(
                    "scorecard series must be cited by the section: " + ", ".join(unknown)
                )
            return
        if self.risk_environment is not None:
            raise ValueError("the risk appetite assessment belongs in the risk environment section")
        if self.scenarios:
            raise ValueError("scenarios belong in the risk environment section")


class ResearchPriorityHint(_SourcedStatement):
    """Which candidate type or sector the hint discriminates.

    Advice that fits every candidate equally is not a hint, so the target it applies
    to is part of the contract.
    """

    applies_to: str = Field(min_length=1)

    @field_validator("applies_to")
    @classmethod
    def require_non_blank_target(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("applies_to must be non-blank")
        return value


class SectorTilt(_SourcedStatement):
    sector: str = Field(min_length=1)
    direction: Literal["supportive", "adverse", "mixed"]


class SizingCaution(_SourcedStatement):
    severity: Literal["low", "medium", "high"]


class MacroConnectionSection(_StrictModel):
    """The single place where the assessment meets the Japanese equity loop."""

    section_id: Literal["japan_equity_loop"]
    series_ids: tuple[str, ...] = Field(min_length=1)
    core_section_ids: tuple[MacroCoreSectionId, ...] = Field(min_length=1)
    fact_summary: tuple[FactSummary, ...] = Field(min_length=1)
    judgment: SectionJudgment
    research_priority_hints: tuple[ResearchPriorityHint, ...] = Field(min_length=1)
    # Optional on purpose: a tilt that no path in the core supports would be a sector
    # ranking invented to fill the field, which is exactly what this layer must not do.
    # The research priority is required because ordering the work is why this section exists.
    sector_tilts: tuple[SectorTilt, ...] = ()
    sizing_cautions: tuple[SizingCaution, ...] = ()

    @model_validator(mode="after")
    def validate_section_contract(self) -> Self:
        if len(self.series_ids) != len(set(self.series_ids)):
            raise ValueError("section series_ids must be unique")
        if len(self.core_section_ids) != len(set(self.core_section_ids)):
            raise ValueError("core_section_ids must be unique")
        return self


class MacroContextDocument(_StrictModel):
    schema_version: Literal[4]
    kind: Literal["macro-context"]
    context_id: str
    as_of: date
    published_at: datetime
    summary: str = Field(min_length=1)
    inputs: MacroInputs
    core: tuple[MacroCoreSection, ...] = Field(min_length=10, max_length=10)
    connection: MacroConnectionSection

    @field_validator("summary")
    @classmethod
    def require_non_blank_summary(cls, value: str) -> str:
        # This is the label the report index renders, so a blank is worse than absent.
        if not value.strip():
            raise ValueError("summary must be non-blank")
        return value

    @model_validator(mode="after")
    def validate_domain_contract(self) -> Self:
        self._validate_context_id()
        if self.published_at.tzinfo is None or self.published_at.utcoffset() is None:
            raise ValueError("published_at must include a timezone")
        if self.published_at.date() < self.as_of:
            raise ValueError("published_at must not predate as_of")
        if tuple(section.section_id for section in self.core) != CORE_SECTION_ORDER:
            raise ValueError("macro context core must contain the fixed ten sections in order")

        statuses, series_input_ids, reading_input_ids = self._index_inputs()
        sections: tuple[MacroCoreSection | MacroConnectionSection, ...] = (
            *self.core,
            self.connection,
        )
        for section in sections:
            self._validate_section_sources(
                section, statuses=statuses, series_input_ids=series_input_ids
            )
        self._validate_reading_citation(reading_input_ids)
        self._validate_connection_references()
        self._validate_scorecard_deadlines()
        if not any(section.material_deltas for section in self.core):
            raise ValueError("a material delta is required")
        return self

    def _validate_context_id(self) -> None:
        match = re.fullmatch(r"macro-context-(\d{4}-\d{2}-\d{2})-[a-z0-9-]+", self.context_id)
        if match is None:
            raise ValueError("context_id has an invalid format")
        # The id is what a human reads in the index, so its date must not disagree with
        # the market date the report is about.
        if match.group(1) != self.as_of.isoformat():
            raise ValueError("context_id date must equal as_of")

    def _index_inputs(self) -> tuple[dict[str, str], dict[str, set[str]], set[str]]:
        statuses: dict[str, str] = {}
        series_input_ids: dict[str, set[str]] = {}
        reading_input_ids: set[str] = set()

        def register(input_id: str, status: str) -> None:
            if input_id in statuses:
                raise ValueError(f"input_id must be unique: {input_id}")
            statuses[input_id] = status

        for article in self.inputs.articles:
            register(article.input_id, article.status)
        for indicator in self.inputs.indicator_series:
            register(indicator.input_id, indicator.status)
            if indicator.series_id not in _canonical_series_ids():
                raise ValueError(f"unregistered macro series_id: {indicator.series_id}")
            series_input_ids.setdefault(indicator.series_id, set()).add(indicator.input_id)
        for reading in self.inputs.reading_snapshots:
            register(reading.input_id, reading.status)
            if reading.reading_asof > self.as_of:
                raise ValueError("a reading snapshot must not be read past the report as_of")
            if (self.as_of - reading.reading_asof).days > MAX_READING_LAG_DAYS:
                raise ValueError(
                    "a reading snapshot must be no more than "
                    f"{MAX_READING_LAG_DAYS} days older than as_of"
                )
            if reading.status == "ok":
                reading_input_ids.add(reading.input_id)
        for snapshot in self.inputs.machine_snapshots:
            register(snapshot.input_id, snapshot.status)
            if snapshot.snapshot_asof > self.as_of:
                raise ValueError("a machine snapshot must not be taken past the report as_of")
            if snapshot.observation_as_of > snapshot.snapshot_asof:
                raise ValueError("a machine snapshot must not observe past its own as_of")
        if not statuses:
            raise ValueError("at least one macro input is required")
        if not reading_input_ids:
            raise ValueError("a successful macro reading snapshot input is required")
        return statuses, series_input_ids, reading_input_ids

    def _validate_section_sources(
        self,
        section: MacroCoreSection | MacroConnectionSection,
        *,
        statuses: dict[str, str],
        series_input_ids: dict[str, set[str]],
    ) -> None:
        items = _sourced_items(section)
        section_source_ids = {source_id for item in items for source_id in item.source_ids}
        for series_id in section.series_ids:
            if series_id not in _canonical_series_ids():
                raise ValueError(f"unregistered macro series_id: {series_id}")
            if series_id not in series_input_ids:
                raise ValueError(f"section series_id has no indicator input: {series_id}")
            ok_input_ids = {
                input_id for input_id in series_input_ids[series_id] if statuses[input_id] == "ok"
            }
            if not ok_input_ids & section_source_ids:
                raise ValueError(
                    f"section series_id has no cited successful indicator input: {series_id}"
                )
        for item in items:
            missing = sorted(set(item.source_ids) - statuses.keys())
            if missing:
                raise ValueError(f"references unknown input IDs: {', '.join(missing)}")
            if any(statuses[source_id] == "failed" for source_id in item.source_ids):
                raise ValueError("an assessment cannot cite failed inputs")

    def _validate_reading_citation(self, reading_input_ids: set[str]) -> None:
        # The regime summary sets the common coordinate for the whole report, so it is
        # the section that has to start from the machine reading.
        cited = {
            source_id for item in _sourced_items(self.core[0]) for source_id in item.source_ids
        }
        if not cited & reading_input_ids:
            raise ValueError("the regime summary must cite a macro reading snapshot input")

    def _validate_connection_references(self) -> None:
        core_series = {series_id for section in self.core for series_id in section.series_ids}
        outside = sorted(set(self.connection.series_ids) - core_series)
        if outside:
            raise ValueError(
                "connection may only cite series the core cites: " + ", ".join(outside)
            )
        # The named core sections must be the ones the connection actually builds on,
        # otherwise the reference is decorative and the trail back into core is lost.
        named = {
            series_id
            for section in self.core
            if section.section_id in set(self.connection.core_section_ids)
            for series_id in section.series_ids
        }
        unbacked = sorted(set(self.connection.series_ids) - named)
        if unbacked:
            raise ValueError(
                "connection core_section_ids must cover the series it cites: " + ", ".join(unbacked)
            )

    def _validate_scorecard_deadlines(self) -> None:
        horizon = _months_after(self.as_of, SCORECARD_HORIZON_MONTHS)
        frequencies = _series_frequencies()
        for scenario in self.scenarios:
            for condition in scenario.scorecard:
                minimum_days = MIN_SCORECARD_DAYS_BY_FREQUENCY.get(
                    frequencies.get(condition.series_id, ""), 14
                )
                if (condition.deadline - self.as_of).days < minimum_days:
                    raise ValueError(
                        f"a scorecard deadline on {condition.series_id} must leave at least "
                        f"{minimum_days} days for the series to print again"
                    )
                if condition.deadline > horizon:
                    raise ValueError(
                        "a scorecard deadline must fall within "
                        f"{SCORECARD_HORIZON_MONTHS} months of as_of"
                    )

    @property
    def scenarios(self) -> tuple[MacroScenario, ...]:
        return self.core[CORE_SECTION_ORDER.index("risk_environment")].scenarios

    @property
    def material_deltas(self) -> tuple[MaterialDelta, ...]:
        return tuple(delta for section in self.core for delta in section.material_deltas)

    @property
    def sizing_cautions(self) -> tuple[SizingCaution, ...]:
        return self.connection.sizing_cautions

    @property
    def research_questions(self) -> tuple[str, ...]:
        return tuple(hint.summary for hint in self.connection.research_priority_hints)

    @property
    def refresh_triggers(self) -> tuple[str, ...]:
        monitoring = self.core[CORE_SECTION_ORDER.index("monitoring")]
        return tuple(point.condition for point in monitoring.monitoring_points)

    def payload(self) -> dict[str, object]:
        return self.model_dump(mode="json")


def _sourced_items(
    section: MacroCoreSection | MacroConnectionSection,
) -> tuple[_SourcedStatement, ...]:
    if isinstance(section, MacroConnectionSection):
        return (
            *section.fact_summary,
            section.judgment,
            *section.research_priority_hints,
            *section.sector_tilts,
            *section.sizing_cautions,
        )
    return (
        *section.fact_summary,
        section.judgment,
        section.economic_connection,
        *section.material_deltas,
        *((section.risk_environment,) if section.risk_environment is not None else ()),
        *section.scenarios,
        *section.monitoring_points,
    )


def _months_after(value: date, months: int) -> date:
    total = value.year * 12 + (value.month - 1) + months
    year, month = divmod(total, 12)
    return date(year, month + 1, min(value.day, monthrange(year, month + 1)[1]))


@lru_cache(maxsize=1)
def _canonical_series_ids() -> frozenset[str]:
    return frozenset(series.series_id for series in load_definitions().series)


@lru_cache(maxsize=1)
def _series_frequencies() -> Mapping[str, str]:
    return {series.series_id: series.frequency for series in load_definitions().series}


__all__ = [
    "CONNECTION_SECTION_ID",
    "CORE_SECTION_ORDER",
    "MACRO_CONTEXT_SCHEMA_VERSION",
    "MATERIAL_DELTA_SECTION_IDS",
    "MacroContextDocument",
    "MacroCoreSectionId",
]
