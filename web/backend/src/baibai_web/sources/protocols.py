"""Domain-vocabulary source contracts for the application read layer."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import date
from typing import Protocol

from baibai_engine.read_api import PortfolioSnapshot
from baibai_web.sources.types import (
    PositionReviewSummary,
    ResearchRevision,
    ScreeningRunRecord,
    TaskRecord,
    ThesisDetail,
)


class LedgerSource(Protocol):
    def exists(self) -> bool: ...

    def snapshot(self) -> PortfolioSnapshot: ...


class MarketPriceSource(Protocol):
    def exists(self) -> bool: ...

    def previous_business_day(self, day: date) -> date | None: ...

    def latest_closes(self, tickers: Sequence[str]) -> Mapping[str, tuple[float, date]]: ...

    def close_changes_since(
        self, tickers: Sequence[str], *, since: date
    ) -> Mapping[str, float]: ...

    def disclosures_after(self, tickers: Sequence[str], *, after: date) -> Mapping[str, date]: ...

    def next_earnings_dates(self, tickers: Sequence[str], *, as_of: date) -> Mapping[str, date]: ...


class ResearchSource(Protocol):
    def revisions(self) -> list[ResearchRevision]: ...

    def thesis_detail(self, thesis_id: str) -> ThesisDetail: ...

    def position_reviews(self, *, ticker: str | None = None) -> list[PositionReviewSummary]: ...

    def research_triages(
        self, *, review_set_ids: Sequence[str] | None = None
    ) -> list[dict[str, object]]: ...

    def assessments(self) -> list[dict[str, object]]: ...

    def assessment(self, capital_allocation_assessment_id: str) -> dict[str, object] | None: ...

    def load_errors(self) -> list[str]: ...


class TaskSource(Protocol):
    def exists(self) -> bool: ...

    def list_tasks(self) -> list[TaskRecord]: ...


class ScreeningSource(Protocol):
    def latest_run(self) -> ScreeningRunRecord | None: ...

    def previous_run(self) -> ScreeningRunRecord | None: ...

    def run(self, run_revision_id: str) -> ScreeningRunRecord | None: ...

    # Canonical newest-first: run as-of, publication instant, then review_set_id.
    def review_sets(self, *, run_revision_id: str | None = None) -> list[dict[str, object]]: ...
