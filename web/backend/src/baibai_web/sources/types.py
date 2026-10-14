"""Storage-neutral values returned by application sources."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime


@dataclass(frozen=True, slots=True)
class ResearchRevision:
    """公開済み Stock Assessment pair 一版の固定要約。"""

    ticker: str
    company_name: str
    sector: str
    summary: str
    as_of: date
    published_at: datetime
    assessment_id: str
    stance: str
    pmax_original_yen: float | None
    review_id: str
    status: str


@dataclass(frozen=True, slots=True)
class StockAssessmentDetail:
    revision: ResearchRevision
    document: dict[str, object]
    review: dict[str, object]
    core_sha256: str
    projection: dict[str, object]


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
