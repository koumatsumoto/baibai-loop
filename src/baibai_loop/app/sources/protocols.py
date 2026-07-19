"""Domain-vocabulary source contracts for the application read layer."""

from __future__ import annotations

from typing import Protocol

from baibai_loop.app.sources.types import (
    CandidatesRun,
    PacketDetail,
    ResearchRevision,
    TaskRecord,
)
from baibai_loop.position.ledger import PortfolioSnapshot


class LedgerSource(Protocol):
    def exists(self) -> bool: ...

    def snapshot(self) -> PortfolioSnapshot: ...


class ResearchSource(Protocol):
    def revisions(self) -> list[ResearchRevision]: ...

    def packet_detail(self, packet_path: str) -> PacketDetail: ...

    def load_errors(self) -> list[str]: ...


class TaskSource(Protocol):
    def exists(self) -> bool: ...

    def list_tasks(self) -> list[TaskRecord]: ...


class CandidatesSource(Protocol):
    def latest_run(self) -> CandidatesRun | None: ...
