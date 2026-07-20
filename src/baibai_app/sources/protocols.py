"""Domain-vocabulary source contracts for the application read layer."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import date
from typing import Protocol

from baibai_app.sources.types import (
    CandidatesRun,
    HoldingReviewSummary,
    PacketDetail,
    ResearchRevision,
    TaskRecord,
)
from baibai_engine.read_api import PortfolioSnapshot


class LedgerSource(Protocol):
    def exists(self) -> bool: ...

    def snapshot(self) -> PortfolioSnapshot: ...


class MarketPriceSource(Protocol):
    def latest_closes(self, tickers: Sequence[str]) -> Mapping[str, tuple[float, date]]: ...

    def next_earnings_dates(self, tickers: Sequence[str], *, asof: date) -> Mapping[str, date]: ...


class MacroContextSource(Protocol):
    def context(self, *, as_of: date) -> Mapping[str, object] | None: ...


class ResearchSource(Protocol):
    def revisions(self) -> list[ResearchRevision]: ...

    def packet_detail(self, packet_id: str) -> PacketDetail: ...

    def holding_reviews(self, *, ticker: str | None = None) -> list[HoldingReviewSummary]: ...

    def load_errors(self) -> list[str]: ...


class TaskSource(Protocol):
    def exists(self) -> bool: ...

    def list_tasks(self) -> list[TaskRecord]: ...


class CandidatesSource(Protocol):
    def latest_run(self) -> CandidatesRun | None: ...
