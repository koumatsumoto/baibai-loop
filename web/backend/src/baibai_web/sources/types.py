"""Storage-neutral values returned by application sources."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime


@dataclass(frozen=True, slots=True)
class ResearchRevision:
    """Summary of one persisted thesis revision."""

    ticker: str
    company_name: str
    sector: str
    as_of: date
    thesis_id: str
    disposition: str
    pmax_raw_yen: float | None
    review_id: str | None
    status: str


@dataclass(frozen=True, slots=True)
class ThesisDetail:
    revision: ResearchRevision
    projection: dict[str, object]


@dataclass(frozen=True, slots=True)
class PositionReviewSummary:
    """Summary of one published position-review revision."""

    position_review_id: str
    ticker: str
    as_of: date
    thesis_id: str
    action: str | None
    note: str | None


@dataclass(frozen=True, slots=True)
class TaskRecord:
    task_id: str
    title: str
    kind: str
    status: str
    ticker: str | None
    due_date: date
    event_label: str | None
    event_date: date | None
    body_md: str | None
    related_refs: tuple[str, ...]
    created_at: date
    closed_at: date | None


@dataclass(frozen=True, slots=True)
class ScreeningRunRecord:
    run_id: str
    as_of: date
    generated_at: datetime
    universe_size: int
    # The revision the machine Review Set and L3 Research Triage bind to;
    # ``run_id`` is the public identifier shown to a reader.
    run_revision_id: str
    # Human-readable rules reference; the path is not a method identity.
    rules_ref: str | None
    # Immutable method identity from the operative run publication. Calibration
    # context is displayable only when both values match its generated artifact.
    screening_rules_hash: str | None
    er_model_version: str | None
    rows: tuple[dict[str, object], ...]


@dataclass(frozen=True, slots=True)
class IndicatorSeriesConfig:
    series_id: str
    # None means "use the series registry name" (single source of truth for labels).
    label: str | None = None


@dataclass(frozen=True, slots=True)
class IndicatorGroupConfig:
    title: str
    series: tuple[IndicatorSeriesConfig, ...]
