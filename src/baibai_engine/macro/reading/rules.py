"""Reading rules: frequency defaults plus per-series overrides.

The rules are a dated revision under ``method/macro-reading/`` so a change to how
values are read is a reviewable revision rather than an edit in code. Resolution
is defaults-by-frequency first, then the series override, so registering a series
never requires touching this file — only a series whose provider makes a default
untrue needs an entry.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import date
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from baibai_engine.foundation.yaml_io import strict_safe_load

DEFAULT_RULES_PATH = Path("method/macro-reading/2026-07-26T113000+0900.yaml")

type FlagComparison = Literal["below", "at_or_below", "above", "at_or_above"]

# What the percentile and z-score measure the position of. ``level`` ranks the value
# itself, which only says something when the level has a scale of its own — a rate, a
# ratio, a diffusion index. ``yoy`` ranks the year-on-year change in percent, for series
# whose scale is set by their own history: an index or a cumulative aggregate sits at the
# 100th percentile every month it keeps rising, so its level percentile reports the
# passage of time while its position lives in the rate of change.
type ReadingStatistic = Literal["level", "yoy"]
# How observations become percentile/z-score sample points. ``raw`` keeps every
# stored date; calendar cadences retain the latest observation in each period.
type SamplingCadence = Literal["raw", "monthly", "quarterly"]
# How the next observation date advances before its publication lag is added.
# This usually matches the registry frequency, but a derived series can retain a
# monthly statistical grid while its current-period value updates every day.
type PublicationCadence = Literal["daily", "weekly", "monthly", "quarterly"]

# Maximum calendar-day span used when deriving a conservative staleness threshold.
# The print estimate itself advances calendar months, so February and month-end dates
# remain calendar-correct.
PUBLICATION_INTERVAL_DAYS: Mapping[str, int] = {
    "daily": 1,
    "weekly": 7,
    "monthly": 31,
    "quarterly": 92,
}


class ReadingRulesError(ValueError):
    """Raised when the reading rules cannot be loaded or resolved."""


class _StrictModel(BaseModel):
    model_config = ConfigDict(frozen=True, strict=True, extra="forbid")


class ThresholdFlag(_StrictModel):
    """A textbook threshold worth noting next to a value (never a signal)."""

    id: str = Field(min_length=1)
    comparison: FlagComparison
    value: float

    def matches(self, value: float) -> bool:
        match self.comparison:
            case "below":
                return value < self.value
            case "at_or_below":
                return value <= self.value
            case "above":
                return value > self.value
            case "at_or_above":
                return value >= self.value


class FrequencyDefaults(_StrictModel):
    percentile_window_years: int = Field(ge=1)
    short_trend_months: int = Field(ge=1)
    long_trend_months: int = Field(ge=1)
    publication_lag_days: int | None = Field(default=None, ge=0)
    staleness_margin_days: int | None = Field(default=None, ge=0)
    staleness_warn_days: int | None = Field(default=None, ge=1)

    @model_validator(mode="after")
    def _require_staleness_inputs(self) -> FrequencyDefaults:
        """Accept old explicit thresholds or the forward-looking current contract."""

        if self.staleness_warn_days is not None:
            return self
        if self.publication_lag_days is None or self.staleness_margin_days is None:
            raise ValueError(
                "defaults require staleness_warn_days or both "
                "publication_lag_days and staleness_margin_days"
            )
        return self


class SeriesOverride(_StrictModel):
    percentile_window_years: int | None = Field(default=None, ge=1)
    statistic: ReadingStatistic | None = None
    sampling_cadence: SamplingCadence | None = None
    short_trend_months: int | None = Field(default=None, ge=1)
    long_trend_months: int | None = Field(default=None, ge=1)
    publication_cadence: PublicationCadence | None = None
    publication_lag_days: int | None = Field(default=None, ge=0)
    staleness_margin_days: int | None = Field(default=None, ge=0)
    staleness_warn_days: int | None = Field(default=None, ge=1)
    flags: tuple[ThresholdFlag, ...] = ()

    @field_validator("flags", mode="before")
    @classmethod
    def _tuple_flags(cls, value: object) -> object:
        # YAML gives a list; strict validation does not coerce sequences.
        return tuple(value) if isinstance(value, list) else value


class ReadingRules(_StrictModel):
    schema_version: Literal[1]
    defaults: Mapping[str, FrequencyDefaults]
    overrides: Mapping[str, SeriesOverride] = {}

    def resolve(self, *, series_id: str, frequency: str) -> ResolvedRule:
        """Resolve the rule for one series, or fail if no default covers it.

        A frequency with no defaults is a gap in the rules, not something to guess
        at, so it raises: a series whose reading rule cannot be resolved must not
        silently get an arbitrary window.
        """

        base = self.defaults.get(frequency)
        if base is None:
            raise ReadingRulesError(
                f"macro reading rules have no defaults for frequency {frequency!r} "
                f"(needed by {series_id})"
            )
        override = self.overrides.get(series_id, SeriesOverride())
        publication_cadence = override.publication_cadence or _publication_cadence(
            frequency,
            series_id=series_id,
        )
        publication_lag_days = _override_or_default(
            override.publication_lag_days,
            base.publication_lag_days,
        )
        staleness_margin_days = _override_or_default(
            override.staleness_margin_days,
            base.staleness_margin_days,
        )
        staleness_warn_days = _override_or_default(
            override.staleness_warn_days,
            base.staleness_warn_days,
        )
        if staleness_warn_days is None:
            if publication_lag_days is None or staleness_margin_days is None:
                raise ReadingRulesError(
                    f"macro reading rules cannot derive staleness for {series_id!r}"
                )
            try:
                publication_interval_days = PUBLICATION_INTERVAL_DAYS[publication_cadence]
            except KeyError as exc:
                raise ReadingRulesError(
                    f"macro reading rules cannot derive a publication interval "
                    f"for cadence {publication_cadence!r} (needed by {series_id})"
                ) from exc
            staleness_warn_days = (
                publication_lag_days + publication_interval_days + staleness_margin_days
            )
        return ResolvedRule(
            percentile_window_years=override.percentile_window_years
            or base.percentile_window_years,
            # The level is the reading a series has unless a rule says its scale is set
            # by its own history, so an unlisted series keeps today's meaning.
            statistic=override.statistic or "level",
            sampling_cadence=override.sampling_cadence or _default_sampling_cadence(frequency),
            short_trend_months=override.short_trend_months or base.short_trend_months,
            long_trend_months=override.long_trend_months or base.long_trend_months,
            publication_cadence=publication_cadence,
            publication_lag_days=publication_lag_days,
            staleness_warn_days=staleness_warn_days,
            flags=override.flags,
        )


class ResolvedRule(_StrictModel):
    percentile_window_years: int
    statistic: ReadingStatistic
    sampling_cadence: SamplingCadence
    short_trend_months: int
    long_trend_months: int
    publication_cadence: PublicationCadence
    publication_lag_days: int | None
    staleness_warn_days: int
    flags: tuple[ThresholdFlag, ...]

    def matched_flags(self, value: float) -> tuple[str, ...]:
        return tuple(flag.id for flag in self.flags if flag.matches(value))


def load_reading_rules(path: Path = DEFAULT_RULES_PATH) -> ReadingRules:
    """Load a revision, rejecting a file that names the same series twice.

    A series is overridden for several unrelated reasons (its window, its statistic, its
    publication lag), so a second entry for one series is an easy edit to make. Plain YAML
    would keep the last one and silently drop the settings above it, which reads as a rule
    that was applied.
    """

    try:
        raw = strict_safe_load(path.read_text(encoding="utf-8"))
    except ValueError as exc:
        raise ReadingRulesError(f"invalid macro reading rules: {path}: {exc}") from exc
    except OSError as exc:
        raise ReadingRulesError(f"failed to read macro reading rules: {path}: {exc}") from exc
    if not isinstance(raw, Mapping):
        raise ReadingRulesError(f"macro reading rules root must be a mapping: {path}")
    return ReadingRules.model_validate(raw)


def rules_revision(path: Path = DEFAULT_RULES_PATH) -> str:
    """The revision identity of a rules file: its dated stem."""

    return path.stem


def _default_sampling_cadence(frequency: str) -> SamplingCadence:
    if frequency == "monthly":
        return "monthly"
    if frequency == "quarterly":
        return "quarterly"
    return "raw"


def _publication_cadence(frequency: str, *, series_id: str) -> PublicationCadence:
    match frequency:
        case "daily" | "weekly" | "monthly" | "quarterly":
            return frequency
        case _:
            raise ReadingRulesError(
                f"macro reading rules cannot derive a publication cadence "
                f"for frequency {frequency!r} (needed by {series_id})"
            )


def _override_or_default[T](override: T | None, default: T | None) -> T | None:
    return default if override is None else override


def window_start(asof: date, years: int) -> date:
    try:
        return asof.replace(year=asof.year - years)
    except ValueError:
        # 2 月 29 日はうるう年でない年に存在しない。
        return asof.replace(year=asof.year - years, day=28)


__all__ = [
    "DEFAULT_RULES_PATH",
    "PUBLICATION_INTERVAL_DAYS",
    "PublicationCadence",
    "ReadingRules",
    "ReadingRulesError",
    "ReadingStatistic",
    "ResolvedRule",
    "SamplingCadence",
    "SeriesOverride",
    "ThresholdFlag",
    "load_reading_rules",
    "rules_revision",
    "window_start",
]
