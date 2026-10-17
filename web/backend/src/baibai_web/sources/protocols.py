"""Domain-vocabulary source contracts for the application read layer."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import date, datetime
from pathlib import Path
from typing import Protocol

from baibai_engine.read_api import PortfolioSnapshot
from baibai_engine.read_api.external_assessment import ExternalAssessment
from baibai_web.sources.types import (
    ResearchRevision,
    ScreeningRunRecord,
    StockAssessmentDetail,
    TaskRecord,
)


class ExternalAssessmentSource(Protocol):
    def assessment(self, ticker: str, *, cutoff: datetime) -> ExternalAssessment: ...
    def observed_tickers(self, *, cutoff: datetime) -> list[str]: ...


class LedgerSource(Protocol):
    def exists(self) -> bool: ...

    def snapshot(self) -> PortfolioSnapshot: ...


class MarketPriceSource(Protocol):
    def exists(self) -> bool: ...

    def market_snapshot(self, *, as_of: date) -> dict[str, object] | None: ...

    def security_market_context(self, ticker: str, *, as_of: date) -> dict[str, object] | None: ...

    def security_identities(self, *, as_of: date) -> Mapping[str, Mapping[str, object]]: ...

    def previous_business_day(self, day: date) -> date | None: ...

    def latest_closes(self, tickers: Sequence[str]) -> Mapping[str, tuple[float, date]]: ...

    def price_comparison_basis_is_confirmed(
        self, ticker: str, *, from_date: date, through_date: date
    ) -> bool: ...

    def close_changes_since(
        self, tickers: Sequence[str], *, since: date
    ) -> Mapping[str, float]: ...

    def disclosures_after(
        self, tickers: Sequence[str], *, after: date, through_date: date
    ) -> Mapping[str, date]: ...

    def next_earnings_dates(self, tickers: Sequence[str], *, as_of: date) -> Mapping[str, date]: ...


class ResearchSource(Protocol):
    def revisions(self) -> list[ResearchRevision]: ...

    def assessment_detail(self, assessment_id: str) -> StockAssessmentDetail | None: ...

    def proposals(self) -> list[dict[str, object]]: ...

    def proposal(self, proposal_id: str) -> dict[str, object] | None: ...

    def proposal_preview(
        self, proposal_id: str, *, sqlite_path: Path, now: datetime
    ) -> dict[str, object] | None: ...


class TaskSource(Protocol):
    def exists(self) -> bool: ...

    def list_tasks(self) -> list[TaskRecord]: ...


class ScreeningSource(Protocol):
    def latest_run(self) -> ScreeningRunRecord | None: ...

    def previous_run(self) -> ScreeningRunRecord | None: ...

    def run(self, run_revision_id: str) -> ScreeningRunRecord | None: ...
