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
    recommendation: str
    confidence: str | None
    current_fair_value_yen: float | None
    model_version: str | None
    review_id: str | None


@dataclass(frozen=True, slots=True)
class ScenarioSummary:
    name: str
    horizon_years: int


@dataclass(frozen=True, slots=True)
class ThesisDetail:
    revision: ResearchRevision
    entry_price_basis_yen: float | None
    required_5y_base_cagr_pct: float | None
    permanent_loss_risk_count: int
    scenarios: tuple[ScenarioSummary, ...]
    permanent_loss_conclusion: str | None
    strongest_countercase: str | None
    sizing_action: str | None


@dataclass(frozen=True, slots=True)
class HoldingReviewSummary:
    """Summary of one published holding-review revision."""

    holding_review_id: str
    ticker: str
    as_of: date
    thesis_id: str
    candidate_thesis_id: str | None
    action: str
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
class CandidatesRun:
    run_id: str
    run_date: date
    asof_date: date
    run_at: datetime
    universe_size: int
    source_path: str
    application_git_commit: str | None
    rows: tuple[dict[str, object], ...]


@dataclass(frozen=True, slots=True)
class MacroSeriesConfig:
    series_id: str
    # None means "use the series registry name" (single source of truth for labels).
    label: str | None = None


@dataclass(frozen=True, slots=True)
class MacroGroupConfig:
    title: str
    series: tuple[MacroSeriesConfig, ...]
