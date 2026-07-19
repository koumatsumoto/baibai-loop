"""Storage-independent source contracts for application reads."""

from .protocols import CandidatesSource, LedgerSource, ResearchSource, TaskSource
from .types import CandidatesRun, PacketDetail, ResearchRevision, ScenarioSummary, TaskRecord

__all__ = [
    "CandidatesRun",
    "CandidatesSource",
    "LedgerSource",
    "PacketDetail",
    "ResearchRevision",
    "ResearchSource",
    "ScenarioSummary",
    "TaskRecord",
    "TaskSource",
]
from .db_sources import DbTaskSource

__all__ = ["DbTaskSource"]
