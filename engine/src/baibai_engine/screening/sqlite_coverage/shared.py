"""Coverage issue record shared by the per-source checks."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class CacheCoverageIssue:
    source: str
    requirement: str
    reason: str
