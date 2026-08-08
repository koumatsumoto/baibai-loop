"""Storage-independent source contracts for application reads."""

from .protocols import CandidatesSource, LedgerSource, ResearchSource, TaskSource
from .types import CandidatesRun, ResearchRevision, ScenarioSummary, TaskRecord, ThesisDetail

__all__ = [
    "CandidatesRun",
    "CandidatesSource",
    "LedgerSource",
    "ResearchRevision",
    "ResearchSource",
    "ScenarioSummary",
    "TaskRecord",
    "TaskSource",
    "ThesisDetail",
]
from .db_sources import DbTaskSource

__all__ = ["DbTaskSource"]
