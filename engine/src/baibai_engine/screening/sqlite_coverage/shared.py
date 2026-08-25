"""Coverage issue record shared by the per-source checks."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class CacheCoverageIssue:
    source: str
    requirement: str
    reason: str
    # Whether screening must not run until this is fixed. An input the run cannot
    # proceed without blocks; an optional axis that goes null when its source is
    # behind is reported and carried, because stopping the whole run over it costs
    # the day's output to protect nothing.
    blocking: bool = True
