"""Storage-independent source contracts for application reads."""

from .protocols import CandidatesSource, LedgerSource, ResearchSource, TaskSource
from .types import CandidatesRun, PacketDetail, ResearchRevision, ScenarioSummary, TaskRecord
from .yaml_sources import (
    YamlCandidatesSource,
    YamlLedgerSource,
    YamlResearchSource,
    YamlTaskSource,
)

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
    "YamlCandidatesSource",
    "YamlLedgerSource",
    "YamlResearchSource",
    "YamlTaskSource",
]
from .db_sources import DbTaskSource

__all__ = ["DbTaskSource"]
