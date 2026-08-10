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
    # The revision the judgment publications (selection / shortlist) bind to;
    # ``run_id`` is the public identifier shown to a reader.
    run_revision_id: str
    # The screening rules the run was built from. A pair of runs with different
    # rules differs by method, not by market, so a delta has to see this.
    rules_ref: str | None
    # Immutable method identity from the operative run publication. Calibration
    # context is displayable only when both values match its generated artifact.
    screening_rules_hash: str | None
    er_model_version: str | None
    rows: tuple[dict[str, object], ...]


@dataclass(frozen=True, slots=True)
class ErLevelCalibrationStats:
    median: float
    q25: float
    q10: float
    trap_rate: float
    n: int


@dataclass(frozen=True, slots=True)
class ErLevelCalibrationBasis:
    basis: str
    ticker_equal: ErLevelCalibrationStats
    cohort_equal: ErLevelCalibrationStats


@dataclass(frozen=True, slots=True)
class ErLevelCalibrationBand:
    band_id: str
    quintile: int | None
    lower_er_annual: float | None
    upper_er_annual: float | None
    median_predicted_er_annual: float
    cohort_count: int
    median_n: int
    bases: tuple[ErLevelCalibrationBasis, ...]


@dataclass(frozen=True, slots=True)
class ErLevelCalibrationHorizon:
    horizon: str
    asof_start: date
    asof_end: date
    cohort_count: int
    bands: tuple[ErLevelCalibrationBand, ...]


@dataclass(frozen=True, slots=True)
class ErLevelCalibrationContext:
    generated_at: datetime
    valid_through: date
    reference_horizon: str
    screening_rules_hash: str
    er_model_version: str
    primary_realized_basis: str
    secondary_realized_basis: str
    trap_basis: str
    horizons: tuple[ErLevelCalibrationHorizon, ...]


@dataclass(frozen=True, slots=True)
class MacroSeriesConfig:
    series_id: str
    # None means "use the series registry name" (single source of truth for labels).
    label: str | None = None


@dataclass(frozen=True, slots=True)
class MacroGroupConfig:
    title: str
    series: tuple[MacroSeriesConfig, ...]
